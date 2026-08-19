// assurance_agent/_resources/opencode/tools/artifact_write.ts
/** Write one bounded workflow artifact without shell or encoding indirection. */
import { lstat, mkdir, realpath, writeFile } from 'fs/promises';
import path from 'path';
import { z } from 'zod';

function isContained(root: string, candidate: string): boolean {
  const relative = path.relative(root, candidate);
  return relative === '' || (!relative.startsWith(`..${path.sep}`) && relative !== '..' && !path.isAbsolute(relative));
}

export default {
  description:
    'Write one complete UTF-8 AA workflow artifact. Use this instead of apply_patch, bash, Python, Base64, or heredocs. ' +
    'The AA boundary plugin enforces the current agent path allowlist.',
  args: {
    path: z.string().min(1).describe('Project-relative allowed artifact path'),
    content: z.string().describe('Complete UTF-8 file content'),
  },
  async execute(
    args: { path: string; content: string },
    context: { directory?: string; worktree?: string },
  ) {
    const suppliedRoot = context.directory ?? context.worktree ?? process.cwd();
    const root = await realpath(suppliedRoot);
    const target = path.resolve(root, args.path);
    if (!isContained(root, target)) throw new Error(`artifact path escapes project root: ${args.path}`);
    if (path.basename(target) === 'workflow-state.json' || path.basename(target) === 'workflow-state.yaml') {
      throw new Error('workflow-state.json is orchestrator-owned');
    }
    await mkdir(path.dirname(target), { recursive: true });
    const parent = await realpath(path.dirname(target));
    if (!isContained(root, parent)) throw new Error(`artifact parent escapes project root: ${args.path}`);
    try {
      if ((await lstat(target)).isSymbolicLink()) throw new Error(`artifact target is a symbolic link: ${args.path}`);
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error;
    }
    await writeFile(target, args.content, { encoding: 'utf-8' });
    return `Wrote ${target}`;
  },
};
