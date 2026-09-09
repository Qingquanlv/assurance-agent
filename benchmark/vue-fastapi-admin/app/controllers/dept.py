from fastapi.exceptions import HTTPException
from tortoise.expressions import Q
from tortoise.transactions import atomic

from app.core.crud import CRUDBase
from app.models.admin import Dept, DeptClosure
from app.schemas.depts import DeptCreate, DeptUpdate


class DeptController(CRUDBase[Dept, DeptCreate, DeptUpdate]):
    def __init__(self):
        super().__init__(model=Dept)

    async def get_dept_tree(self, name):
        q = Q()
        # 获取所有未被软删除的部门
        q &= Q(is_deleted=False)
        if name:
            q &= Q(name__contains=name)
        all_depts = await self.model.filter(q).order_by("order")

        # 辅助函数，用于递归构建部门树
        def build_tree(parent_id):
            return [
                {
                    "id": dept.id,
                    "name": dept.name,
                    "desc": dept.desc,
                    "order": dept.order,
                    "parent_id": dept.parent_id,
                    "children": build_tree(dept.id),  # 递归构建子部门
                }
                for dept in all_depts
                if dept.parent_id == parent_id
            ]

        # 从顶级部门（parent_id=0）开始构建部门树
        dept_tree = build_tree(0)
        return dept_tree

    async def get_dept_info(self):
        pass

    async def name_taken(self, name: str, *, exclude_id: int | None = None) -> bool:
        query = self.model.filter(name=name, is_deleted=False)
        if exclude_id is not None:
            query = query.exclude(id=exclude_id)
        return await query.exists()

    async def ensure_self_closure(self, dept_id: int) -> None:
        exists = await DeptClosure.filter(ancestor=dept_id, descendant=dept_id).exists()
        if not exists:
            await DeptClosure.create(ancestor=dept_id, descendant=dept_id, level=0)

    async def update_dept_closure(self, obj: Dept):
        if obj.parent_id != 0:
            await self.ensure_self_closure(obj.parent_id)
        parent_depts = await DeptClosure.filter(descendant=obj.parent_id)
        dept_closure_objs: list[DeptClosure] = []
        for item in parent_depts:
            dept_closure_objs.append(DeptClosure(ancestor=item.ancestor, descendant=obj.id, level=item.level + 1))
        dept_closure_objs.append(DeptClosure(ancestor=obj.id, descendant=obj.id, level=0))
        await DeptClosure.bulk_create(dept_closure_objs)

    @atomic()
    async def create_dept(self, obj_in: DeptCreate):
        if await self.name_taken(obj_in.name):
            raise HTTPException(status_code=400, detail="The department with this name already exists in the system.")
        if obj_in.parent_id != 0:
            await self.get(id=obj_in.parent_id)
        new_obj = await self.create(obj_in=obj_in)
        await self.update_dept_closure(new_obj)

    @atomic()
    async def update_dept(self, obj_in: DeptUpdate):
        dept_obj = await self.get(id=obj_in.id)
        if await self.name_taken(obj_in.name, exclude_id=obj_in.id):
            raise HTTPException(status_code=400, detail="The department with this name already exists in the system.")
        if dept_obj.parent_id != obj_in.parent_id:
            if obj_in.parent_id != 0:
                await self.get(id=obj_in.parent_id)
            await DeptClosure.filter(ancestor=dept_obj.id).delete()
            await DeptClosure.filter(descendant=dept_obj.id).delete()
            dept_obj.parent_id = obj_in.parent_id
            await self.update_dept_closure(dept_obj)
        dept_obj.update_from_dict(obj_in.model_dump(exclude_unset=True))
        await dept_obj.save()

    @atomic()
    async def delete_dept(self, dept_id: int):
        # 删除部门
        obj = await self.get(id=dept_id)
        obj.is_deleted = True
        await obj.save()
        # 删除关系
        await DeptClosure.filter(descendant=dept_id).delete()


dept_controller = DeptController()
