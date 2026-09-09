import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const BINDING_PREFIX = "aa-workspace-binding-v1:";
const REQUIRED_KEYS = [
  "agent_profile",
  "allowed_outputs",
  "attempt",
  "attempt_id",
  "digest",
  "project_root_digest",
  "read_roots",
  "schema_version",
  "session_id",
  "task_id",
  "write_root",
];
const WRITE_TOOLS = new Set(["apply_patch", "artifact_write", "edit", "write"]);
const READ_TOOLS = new Set(["read", "glob", "grep"]);
const SHELL_TOOLS = new Set(["bash", "interactive_bash"]);
const EXECUTOR_PROFILE = "assurance-v1-executor";
const EXECUTION_VIEW_RELATIVE = /^qa\/changes\/[^/]+\/\.staging\/execution\/[^/]+$/;
const HYPOTHESIS_CACHE = /^\/tmp\/aa-hypothesis-[A-Za-z0-9._-]+$/;
const SHELL_UNSAFE = /[;\n\r`<>]|&&|\|\||(?<!\$)\||\$\(|<\(|>\(/;
const EXECUTION_VIEW_PATTERN = String.raw`(?:[A-Za-z0-9._-]+/)*qa/changes/[A-Za-z0-9._-]+/\.staging/execution/[A-Za-z0-9._-]+`;
const EXECUTION_VIEW_FILE_PATTERN = String.raw`${EXECUTION_VIEW_PATTERN}/[A-Za-z0-9._/-]+`;
const HYPOTHESIS_PATTERN = String.raw`/tmp/aa-hypothesis-[A-Za-z0-9._-]+`;
const PLAYWRIGHT_OUTPUT = /^\/tmp\/aa-playwright-[A-Za-z0-9._-]+$/;
const EXECUTOR_GRAMMARS = [
  new RegExp(String.raw`^npm run test --prefix ${EXECUTION_VIEW_PATTERN}(?:\s+\S+)*$`),
  new RegExp(String.raw`^npm test --prefix ${EXECUTION_VIEW_PATTERN}(?:\s+\S+)*$`),
  new RegExp(String.raw`^npx playwright test --config=${EXECUTION_VIEW_PATTERN}(?:\s+\S+)*$`),
  new RegExp(String.raw`^pnpm --dir ${EXECUTION_VIEW_PATTERN} run test(?:\s+\S+)*$`),
  new RegExp(String.raw`^pnpm --dir ${EXECUTION_VIEW_PATTERN} test(?:\s+\S+)*$`),
  new RegExp(
    String.raw`^PYTHONDONTWRITEBYTECODE=1 HYPOTHESIS_STORAGE_DIRECTORY=${HYPOTHESIS_PATTERN} uv run --isolated pytest -p no:cacheprovider --tb=line --rootdir ${EXECUTION_VIEW_PATTERN}(?:\s+\S+)*$`,
  ),
  new RegExp(
    String.raw`^PYTHONDONTWRITEBYTECODE=1 uv run --isolated locust --locustfile ${EXECUTION_VIEW_FILE_PATTERN}(?:\s+\S+)*$`,
  ),
];
const PATCH_HEADERS = /^(Add File|Update File|Delete File|Move to): /;

const canonicalJson = (value) => {
  if (value === null || typeof value === "number" || typeof value === "boolean") {
    return JSON.stringify(value);
  }
  if (typeof value === "string") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (typeof value === "object") {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`).join(",")}}`;
  }
  throw new Error("Assurance write boundary: binding is missing or invalid");
};

const canonicalDigest = (value) => (
  crypto.createHash("sha256").update(canonicalJson(value), "utf8").digest("hex")
);

const canonicalize = (candidate, root) => {
  let value = candidate;
  if (/^file:/i.test(value)) value = fileURLToPath(value);
  else if (/^[a-z][a-z0-9+.-]*:\/\//i.test(value)) throw new Error("non-file URL path");
  if (value.includes("\0")) throw new Error("NUL path");

  const absolute = path.isAbsolute(value) ? path.normalize(value) : path.resolve(root, value);
  const missing = [];
  let existing = absolute;
  while (true) {
    try {
      fs.lstatSync(existing);
      break;
    } catch (error) {
      if (!(error instanceof Error) || error.code !== "ENOENT") throw error;
      const parent = path.dirname(existing);
      if (parent === existing) throw error;
      missing.unshift(path.basename(existing));
      existing = parent;
    }
  }
  const resolved = fs.realpathSync.native(existing);
  return path.resolve(resolved, ...missing);
};

const relativeLogical = (root, candidate) => {
  const canonical = canonicalize(candidate, root);
  const relative = path.relative(root, canonical);
  if (relative === ".." || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)) {
    throw new Error("Assurance write boundary: path is not allowed");
  }
  return relative.split(path.sep).join("/");
};

const isSymlink = (candidate) => {
  try {
    return fs.lstatSync(candidate).isSymbolicLink();
  } catch (error) {
    if (!(error instanceof Error) || error.code !== "ENOENT") throw error;
    return false;
  }
};

const exists = (candidate) => {
  try {
    fs.lstatSync(candidate);
    return true;
  } catch (error) {
    if (!(error instanceof Error) || error.code !== "ENOENT") throw error;
    return false;
  }
};

const projectRelative = (value) => {
  if (typeof value !== "string" || value.length === 0 || value.startsWith("/") || value.includes("\\")) {
    return false;
  }
  return !value.split("/").some((part) => part === "" || part === "." || part === "..");
};

const uniqueSorted = (values) => (
  values.every((value, index) => index === 0 || values[index - 1] < value)
);

const parseBinding = (session, root) => {
  if (typeof session?.title !== "string" || !session.title.startsWith(BINDING_PREFIX)) {
    throw new Error("Assurance write boundary: binding is missing or invalid");
  }
  if (typeof session.id !== "string" || session.id.length === 0) {
    throw new Error("Assurance write boundary: binding is missing or invalid");
  }
  let document;
  try {
    document = JSON.parse(session.title.slice(BINDING_PREFIX.length));
  } catch {
    throw new Error("Assurance write boundary: binding is missing or invalid");
  }
  if (document === null || typeof document !== "object" || Array.isArray(document)) {
    throw new Error("Assurance write boundary: binding is missing or invalid");
  }
  if (Object.keys(document).sort().join(",") !== REQUIRED_KEYS.join(",")) {
    throw new Error("Assurance write boundary: binding is missing or invalid");
  }
  const { digest, ...payload } = document;
  if (
    document.schema_version !== "1"
    || typeof digest !== "string"
    || digest !== canonicalDigest(payload)
    || document.session_id !== session.id
    || document.agent_profile !== session.agent
    || document.project_root_digest !== canonicalDigest(root)
    || typeof document.write_root !== "string"
    || !projectRelative(document.write_root)
    || !Array.isArray(document.allowed_outputs)
    || !document.allowed_outputs.every((item) => typeof item === "string" && projectRelative(item))
    || !uniqueSorted(document.allowed_outputs)
    || !Array.isArray(document.read_roots)
    || !document.read_roots.every((item) => typeof item === "string" && projectRelative(item))
    || !uniqueSorted(document.read_roots)
    || typeof document.task_id !== "string"
    || document.task_id.length === 0
    || !Number.isInteger(document.attempt)
    || document.attempt < 1
    || typeof document.attempt_id !== "string"
    || document.attempt_id.length === 0
  ) {
    throw new Error("Assurance write boundary: binding is missing or invalid");
  }
  return document;
};

const stagedPhysical = (root, writeRoot, logical) => path.resolve(root, writeRoot, logical);

const lexicalLogical = (root, candidate) => {
  let value = candidate;
  if (/^file:/i.test(value)) value = fileURLToPath(value);
  else if (/^[a-z][a-z0-9+.-]*:\/\//i.test(value)) throw new Error("non-file URL path");
  if (value.includes("\0")) throw new Error("NUL path");
  const absolute = path.isAbsolute(value) ? path.normalize(value) : path.resolve(root, value);
  const relative = path.relative(root, absolute);
  if (relative === "" || relative === ".." || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)) {
    throw new Error("Assurance write boundary: path is not allowed");
  }
  return { absolute, logical: relative.split(path.sep).join("/") };
};

const assertWritable = (binding, root, candidate) => {
  const { absolute, logical } = lexicalLogical(root, candidate);
  const leaf = logical.split("/").at(-1);
  if (logical === "tests" || logical.startsWith("tests/")) {
    throw new Error("Assurance write boundary: path is not allowed");
  }
  if (leaf === "workflow-state.json" || leaf === "workflow-state.yaml") {
    throw new Error("Assurance write boundary: path is not allowed");
  }
  if (binding.read_roots.some((item) => logical === item || logical.startsWith(`${item}/`))) {
    throw new Error("Assurance write boundary: path is not allowed");
  }
  if (!binding.allowed_outputs.includes(logical)) {
    throw new Error("Assurance write boundary: path is not allowed");
  }
  const dest = stagedPhysical(root, binding.write_root, logical);
  if (isSymlink(absolute) || isSymlink(dest)) {
    throw new Error("Assurance write boundary: symlink write is not allowed");
  }
  return dest;
};

const lstatOrNull = (candidate) => {
  try {
    return fs.lstatSync(candidate);
  } catch (error) {
    if (error instanceof Error && error.code === "ENOENT") return null;
    throw error;
  }
};

const assertPrivateRegular = (root, candidate, stats) => {
  if (!stats.isFile() || stats.isSymbolicLink() || stats.nlink !== 1) {
    throw new Error("Assurance write boundary: copy-on-write source is not allowed");
  }
  const resolved = fs.realpathSync.native(candidate);
  const relative = path.relative(root, resolved);
  if (
    resolved !== candidate
    || relative === ".."
    || relative.startsWith(`..${path.sep}`)
    || path.isAbsolute(relative)
  ) {
    throw new Error("Assurance write boundary: copy-on-write source is not allowed");
  }
};

const ensurePrivateDirectories = (root, destination) => {
  const relative = path.relative(root, destination);
  if (relative === ".." || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)) {
    throw new Error("Assurance write boundary: copy-on-write destination is not allowed");
  }
  let current = root;
  for (const segment of relative.split(path.sep).filter(Boolean)) {
    current = path.join(current, segment);
    const existing = lstatOrNull(current);
    if (existing === null) {
      fs.mkdirSync(current, { mode: 0o700 });
      continue;
    }
    if (!existing.isDirectory() || existing.isSymbolicLink() || fs.realpathSync.native(current) !== current) {
      throw new Error("Assurance write boundary: copy-on-write destination is not allowed");
    }
  }
};

const copyBaselineForMutation = (root, candidate, destination) => {
  const { absolute } = lexicalLogical(root, candidate);
  const destinationStats = lstatOrNull(destination);
  if (destinationStats !== null) {
    assertPrivateRegular(root, destination, destinationStats);
    return;
  }
  const sourceStats = lstatOrNull(absolute);
  if (sourceStats === null) return;
  assertPrivateRegular(root, absolute, sourceStats);

  const sourceFlags = fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW ?? 0);
  const source = fs.openSync(absolute, sourceFlags);
  try {
    const opened = fs.fstatSync(source);
    if (
      !opened.isFile()
      || opened.nlink !== 1
      || opened.dev !== sourceStats.dev
      || opened.ino !== sourceStats.ino
    ) {
      throw new Error("Assurance write boundary: copy-on-write source is not allowed");
    }
    const contents = fs.readFileSync(source);
    const observed = fs.fstatSync(source);
    if (
      observed.dev !== opened.dev
      || observed.ino !== opened.ino
      || observed.nlink !== 1
      || observed.size !== opened.size
      || observed.mtimeMs !== opened.mtimeMs
    ) {
      throw new Error("Assurance write boundary: copy-on-write source changed during read");
    }

    ensurePrivateDirectories(root, path.dirname(destination));
    const mode = opened.mode & 0o777;
    const destinationFlags = fs.constants.O_WRONLY
      | fs.constants.O_CREAT
      | fs.constants.O_EXCL
      | (fs.constants.O_NOFOLLOW ?? 0);
    const staged = fs.openSync(destination, destinationFlags, mode);
    try {
      fs.writeFileSync(staged, contents);
      fs.fchmodSync(staged, mode);
    } finally {
      fs.closeSync(staged);
    }
  } finally {
    fs.closeSync(source);
  }
};

const rewritePatch = (text, rewrite) => {
  if (typeof text !== "string") throw new Error("apply_patch requires patchText");
  const lines = text.replace(/\r\n/g, "\n").split("\n");
  if (lines[0] !== "*** Begin Patch" || lines.at(-1) !== "*** End Patch") {
    throw new Error("apply_patch has an invalid envelope");
  }
  let operations = 0;
  const rewritten = [lines[0]];
  for (const line of lines.slice(1, -1)) {
    if (!line.startsWith("*** ")) {
      rewritten.push(line);
      continue;
    }
    const body = line.slice(4);
    const match = body.match(PATCH_HEADERS);
    if (match === null) throw new Error("apply_patch has an invalid file header");
    const target = body.slice(match[0].length);
    if (target.trim() !== target || target.length === 0) {
      throw new Error("apply_patch has an invalid file path");
    }
    rewritten.push(`*** ${match[0]}${rewrite(target, match[1])}`);
    if (match[1] !== "Move to") operations += 1;
  }
  rewritten.push(lines.at(-1));
  if (operations === 0) throw new Error("apply_patch declares no file operation");
  return rewritten.join("\n");
};

const nativeWriteKeys = ["filePath", "file_path", "path", "rename"];

const rewriteNativeWrites = (args, rewrite) => {
  let found = false;
  for (const key of nativeWriteKeys) {
    if (typeof args?.[key] === "string" && args[key].length > 0) {
      args[key] = rewrite(args[key]);
      found = true;
    }
  }
  if (!found) throw new Error("write tool has no file path");
};

const tokenizeShell = (command) => {
  const tokens = [];
  const pattern = /"([^"]*)"|'([^']*)'|(\S+)/g;
  let match = pattern.exec(command);
  while (match !== null) {
    tokens.push(match[1] ?? match[2] ?? match[3]);
    match = pattern.exec(command);
  }
  return tokens;
};

const looksLikePath = (value) => value.includes("/") || value.includes("\\") || value === "." || value === "..";

const assignmentValue = (token) => {
  const eq = token.indexOf("=");
  if (eq <= 0) return null;
  let value = token.slice(eq + 1);
  while (value.includes("=")) {
    value = value.slice(value.indexOf("=") + 1);
  }
  return value;
};

const pathCandidates = (command) => {
  const found = [];
  for (const token of tokenizeShell(command)) {
    if (token.includes("=")) {
      const value = assignmentValue(token);
      if (value !== null && looksLikePath(value)) found.push(value);
      continue;
    }
    if (looksLikePath(token)) found.push(token);
  }
  return found;
};

const OUTPUT_FLAG = /(?:output|file|log|html|xml|dir|cache|basetemp|junit|result)$/i;

const isOutputDestinationFlag = (token) => (
  token.startsWith("-") && !token.includes("=") && OUTPUT_FLAG.test(token.replace(/^-+/, ""))
);

const boundExecutionView = (binding) => {
  const prefix = `${binding.write_root}/`;
  const candidates = binding.read_roots.filter((item) => (
    item.startsWith(prefix) && EXECUTION_VIEW_RELATIVE.test(item.slice(prefix.length))
  ));
  return candidates.length === 1 ? candidates[0] : null;
};

const projectCommandPath = (value, root) => {
  const candidate = value.split("::", 1)[0];
  if (path.isAbsolute(candidate) || /^file:/i.test(candidate)) {
    try {
      return relativeLogical(root, candidate);
    } catch {
      return null;
    }
  }
  return projectRelative(candidate) ? candidate : null;
};

const isAllowedOutputTarget = (value, root, binding) => (
  HYPOTHESIS_CACHE.test(value)
  || PLAYWRIGHT_OUTPUT.test(value)
  || isExecutionViewPath(value, root, binding)
);

const isExecutionViewPath = (value, root, binding) => {
  const expected = boundExecutionView(binding);
  const relative = projectCommandPath(value, root);
  return expected !== null
    && relative !== null
    && (relative === expected || relative.startsWith(`${expected}/`));
};

const outputParents = (binding) => new Set(
  binding.allowed_outputs
    .map((item) => item.split("/").slice(0, -1).join("/"))
    .filter((parent) => parent && !parent.includes(".staging/execution/")),
);

const isDeniedLocation = (value, root, binding) => {
  if (value === root || value === `${root}/` || value === `${root}\\`) return true;
  const relative = value.startsWith(`${root}/`)
    ? value.slice(root.length + 1)
    : value.startsWith(`${root}\\`)
      ? value.slice(root.length + 1).split("\\").join("/")
      : value;
  if (relative === "" || relative === ".") return true;
  for (const parent of outputParents(binding)) {
    if (relative === parent || value === parent) return true;
  }
  return !isExecutionViewPath(value, root, binding)
    && !HYPOTHESIS_CACHE.test(value)
    && !PLAYWRIGHT_OUTPUT.test(value);
};

const matchesExecutorGrammar = (command) => EXECUTOR_GRAMMARS.some((pattern) => pattern.test(command));

const isCwdRelativeOutput = (token, root, binding) => {
  const value = assignmentValue(token);
  if (value === null) {
    return token === "." || token === ".." || token.startsWith("./") || token.startsWith("../");
  }
  if (!value) return true;
  if (isAllowedOutputTarget(value, root, binding)) {
    return false;
  }
  const lastEq = token.lastIndexOf("=");
  const before = token.slice(0, lastEq);
  const prevEq = before.lastIndexOf("=");
  const key = prevEq >= 0 ? before.slice(prevEq + 1) : before;
  return OUTPUT_FLAG.test(key) || looksLikePath(value);
};

const hasDeniedTwoTokenOutput = (tokens, root, binding) => {
  for (let index = 0; index < tokens.length; index += 1) {
    if (!isOutputDestinationFlag(tokens[index])) continue;
    const next = tokens[index + 1];
    if (next === undefined || next.startsWith("-") || !isAllowedOutputTarget(next, root, binding)) {
      return true;
    }
  }
  return false;
};

const assertExecutorShell = (binding, root, args) => {
  if (binding.agent_profile !== EXECUTOR_PROFILE) {
    throw new Error("Assurance write boundary: shell escape is not allowed");
  }
  const command = typeof args?.command === "string" ? args.command : "";
  if (!command || SHELL_UNSAFE.test(command) || !matchesExecutorGrammar(command)) {
    throw new Error("Assurance write boundary: shell escape is not allowed");
  }
  const tokens = tokenizeShell(command);
  if (tokens.some((token) => token === "-c" || /^python\d*$/.test(token))) {
    throw new Error("Assurance write boundary: shell escape is not allowed");
  }
  if (
    tokens.some((token) => isCwdRelativeOutput(token, root, binding))
    || hasDeniedTwoTokenOutput(tokens, root, binding)
  ) {
    throw new Error("Assurance write boundary: shell escape is not allowed");
  }
  const paths = pathCandidates(command);
  if (paths.some((item) => isDeniedLocation(item, root, binding))) {
    throw new Error("Assurance write boundary: shell escape is not allowed");
  }
};

const rewriteRead = (tool, args, binding, root) => {
  const keys = tool === "read" ? ["filePath", "file_path", "path"] : ["path"];
  let target = null;
  let key = null;
  for (const candidate of keys) {
    if (typeof args?.[candidate] === "string" && args[candidate].length > 0) {
      target = args[candidate];
      key = candidate;
      break;
    }
  }
  if (target === null) {
    if (tool === "read") throw new Error("read tool has no file path");
    return;
  }
  const logical = relativeLogical(root, target);
  if (
    binding.agent_profile === EXECUTOR_PROFILE
    && !isExecutionViewPath(logical, root, binding)
  ) {
    throw new Error("Assurance path boundary: path is not allowed");
  }
  if (logical === "") {
    args[key] = root;
    return;
  }
  const staged = stagedPhysical(root, binding.write_root, logical);
  if (exists(staged) && !isSymlink(staged)) {
    args[key] = staged;
    return;
  }
  args[key] = path.resolve(root, logical);
};

export default async ({ client }) => ({
  tool: {
    assurance_boundary_v1: {
      description: "Assurance bounded-write guard marker",
      args: {},
      async execute() {
        return "assurance-boundary-v1";
      },
    },
  },
  "tool.execute.before": async (input, output) => {
    const tool = input.tool.toLowerCase();
    if (!WRITE_TOOLS.has(tool) && !READ_TOOLS.has(tool) && !SHELL_TOOLS.has(tool)) return;
    if (typeof client?.session?.get !== "function") {
      throw new Error("Assurance path boundary: session lookup unavailable");
    }
    const response = await client.session.get({ path: { id: input.sessionID } });
    const session = response?.data ?? response;
    if (!session || typeof session.directory !== "string" || typeof session.agent !== "string") {
      throw new Error("Assurance path boundary: session identity is missing");
    }
    const root = canonicalize(session.directory, session.directory);
    const binding = parseBinding(session, root);
    if (SHELL_TOOLS.has(tool)) {
      if (tool !== "bash") {
        throw new Error("Assurance write boundary: shell escape is not allowed");
      }
      assertExecutorShell(binding, root, output.args);
      return;
    }
    if (READ_TOOLS.has(tool)) {
      rewriteRead(tool, output.args, binding, root);
      return;
    }
    const rewrite = (candidate, copyBaseline = false) => {
      const destination = assertWritable(binding, root, candidate);
      if (copyBaseline) copyBaselineForMutation(root, candidate, destination);
      return destination;
    };
    if (tool === "apply_patch") {
      output.args.patchText = rewritePatch(
        output.args?.patchText,
        (candidate, operation) => rewrite(
          candidate,
          operation === "Update File" || operation === "Delete File",
        ),
      );
      return;
    }
    rewriteNativeWrites(output.args, (candidate) => rewrite(candidate, tool === "edit"));
  },
});
