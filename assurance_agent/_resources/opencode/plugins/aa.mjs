// assurance_agent/_resources/opencode/plugins/aa.mjs
/**
 * AA (Assurance Agent) plugin for OpenCode.ai
 *
 * Registers the AA skills directory so OpenCode discovers all QA workflow
 * skills without symlinks or manual config. Skills live at <project>/skills/
 * (laid down by `aa skill refresh` / `aa init`), two levels up from
 * .opencode/plugins/.
 */
import path from 'path';
import fs from 'fs';
import { createHash } from 'crypto';
import { fileURLToPath } from 'url';

const AA_PLUGIN_PATH = fileURLToPath(import.meta.url);
const __dirname = path.dirname(AA_PLUGIN_PATH);
const AA_BOUNDARY_PROBE_PREFIX = 'aa_boundary_probe_v1_';
const AA_BOUNDARY_PLUGIN_SHA256 = createHash('sha256')
  .update(fs.readFileSync(AA_PLUGIN_PATH))
  .digest('hex');
const AA_BOUNDARY_PROBE_TOOL = `${AA_BOUNDARY_PROBE_PREFIX}${AA_BOUNDARY_PLUGIN_SHA256}`;

// Skills live two levels up from .opencode/plugins/
const AA_SKILLS_DIR = path.resolve(__dirname, '../../skills');

const AA_BOUNDED_AGENTS = new Set([
  'aa-archiver',
  'aa-doc-author',
  'aa-explorer',
  'aa-intake-host',
  'aa-reporter',
  'aa-reviewer',
  'aa-test-author',
]);

// This is the runtime authority. Agent frontmatter still carries explicit
// denies so stale live catalogs are visible during preflight, but a denylist
// alone cannot anticipate new OMO experimental tools (for example task_*).
// Bounded AA personas therefore get only this closed capability surface.
const BOUNDED_AGENT_ALLOWED_TOOLS = new Set([
  'ast_grep_replace',
  'ast_grep_search',
  'bash',
  'edit',
  'glob',
  'grep',
  'list',
  'lsp_diagnostics',
  'lsp_find_references',
  'lsp_goto_definition',
  'lsp_prepare_rename',
  'lsp_rename',
  'lsp_symbols',
  'question',
  'read',
  'skill',
  'todoread',
  'todowrite',
  'workflow_start',
  'write',
]);

const INTAKE_HOST_SKILLS = new Set([
  'aa-intake',
  'aa-explore',
  'aa-case-design',
  'aa-case-reviewer',
  'aa-case-fixer',
]);

// OMO supplies its own grep/glob implementations. Those tools spawn rg
// directly, so OpenCode's native external_directory permission is never
// consulted. Keep a second, tool-agnostic filesystem boundary at the plugin
// seam for every path-bearing tool available to bounded AA agents.
const FILESYSTEM_TOOL_PATH_FIELDS = new Map([
  ['grep', ['path']],
  ['glob', ['path']],
  ['list', ['path']],
  ['read', ['filePath', 'file_path', 'path']],
  ['write', ['filePath', 'file_path', 'path']],
  ['edit', ['filePath', 'file_path', 'path', 'rename']],
  ['ast_grep_search', ['paths']],
  ['ast_grep_replace', ['paths']],
  ['look_at', ['file_path', 'file_paths']],
  ['lsp_diagnostics', ['filePath', 'file_path']],
  ['lsp_symbols', ['filePath', 'file_path']],
  ['lsp_goto_definition', ['filePath', 'file_path']],
  ['lsp_find_references', ['filePath', 'file_path']],
  ['lsp_prepare_rename', ['filePath', 'file_path']],
  ['lsp_rename', ['filePath', 'file_path']],
]);

const IMPLICIT_SEARCH_ROOT_FIELDS = new Map([
  ['grep', 'path'],
  ['glob', 'path'],
  ['list', 'path'],
  ['ast_grep_search', 'paths'],
  ['ast_grep_replace', 'paths'],
]);

const canonicalizePath = (candidate, root) => {
  let filesystemPath = candidate;
  if (/^file:/i.test(candidate)) {
    filesystemPath = fileURLToPath(candidate);
  } else if (/^[a-z][a-z0-9+.-]*:\/\//i.test(candidate)) {
    throw new Error('path uses a non-file URL scheme');
  }

  if (filesystemPath.includes('\0')) {
    throw new Error('path contains a NUL byte');
  }

  const absolute = path.isAbsolute(filesystemPath)
    ? path.normalize(filesystemPath)
    : path.resolve(root, filesystemPath);
  const missingSegments = [];
  let existing = absolute;
  while (true) {
    try {
      fs.lstatSync(existing);
      break;
    } catch (error) {
      if (!(error instanceof Error) || error.code !== 'ENOENT') throw error;
      const parent = path.dirname(existing);
      if (parent === existing) throw error;
      missingSegments.unshift(path.basename(existing));
      existing = parent;
    }
  }

  // realpath must run after lstat found the nearest existing entry. A dangling
  // symlink exists for lstat but fails realpath, so it is rejected instead of
  // being mistaken for an ordinary not-yet-created path segment.
  const realExisting = fs.realpathSync.native(existing);
  return path.resolve(realExisting, ...missingSegments);
};

const isContainedPath = (root, candidate) => {
  const relative = path.relative(root, candidate);
  return relative === '' || (!relative.startsWith(`..${path.sep}`) && relative !== '..' && !path.isAbsolute(relative));
};

const assertGlobCannotFollowEscapingSymlink = (
  searchRoot,
  boundaryRoot,
  hostRoot,
) => {
  if (!fs.existsSync(searchRoot)) return;

  const runtimeDirectoryNames = new Set(['.opencode', '.venv', 'node_modules']);
  const authorizedHostLink = (entryPath, target) => {
    const relative = path.relative(boundaryRoot, entryPath);
    const segments = relative.split(path.sep);
    const isRuntimeDirectory = segments.length === 1 && runtimeDirectoryNames.has(segments[0]);
    const isChangeLedger = (
      segments.length === 4
      && segments[0] === 'qa'
      && segments[1] === 'changes'
      && segments[2].length > 0
      && segments[3] === 'events.jsonl'
    );
    if (!isRuntimeDirectory && !isChangeLedger) return null;

    // The host entry itself must be an ordinary file/directory at the exact
    // server-root-relative location.  A second symlink at the host side must
    // not turn a trusted task link into an arbitrary external traversal.
    const expected = path.resolve(hostRoot, relative);
    let expectedStat;
    try {
      expectedStat = fs.lstatSync(expected);
    } catch {
      return null;
    }
    if (expectedStat.isSymbolicLink() || path.normalize(target) !== expected) return null;
    if (isRuntimeDirectory && !expectedStat.isDirectory()) return null;
    if (isChangeLedger && !expectedStat.isFile()) return null;
    return expected;
  };

  const pending = [{ directory: searchRoot, permittedRoot: boundaryRoot }];
  const visited = new Set();
  while (pending.length > 0) {
    const { directory, permittedRoot } = pending.pop();
    const canonicalDirectory = fs.realpathSync.native(directory);
    if (!isContainedPath(permittedRoot, canonicalDirectory)) {
      throw new Error(`directory resolves outside the session: ${directory}`);
    }
    const visitKey = `${permittedRoot}\0${canonicalDirectory}`;
    if (visited.has(visitKey)) continue;
    visited.add(visitKey);

    for (const entry of fs.readdirSync(canonicalDirectory, { withFileTypes: true })) {
      const entryPath = path.join(canonicalDirectory, entry.name);
      if (entry.isSymbolicLink()) {
        const target = fs.realpathSync.native(entryPath);
        let targetBoundary = permittedRoot;
        if (!isContainedPath(permittedRoot, target)) {
          const authorizedTarget = permittedRoot === boundaryRoot
            ? authorizedHostLink(entryPath, target)
            : null;
          if (authorizedTarget === null) {
            throw new Error(`symbolic link resolves outside the session: ${entryPath}`);
          }
          targetBoundary = authorizedTarget;
        }
        if (fs.statSync(target).isDirectory()) {
          pending.push({ directory: target, permittedRoot: targetBoundary });
        }
      } else if (entry.isDirectory()) {
        pending.push({ directory: entryPath, permittedRoot });
      }
    }
  }
};

const filterGlobOutputToSession = (rawOutput, boundaryRoot) => {
  if (typeof rawOutput !== 'string') throw new Error('glob returned a non-string result');
  if (rawOutput === 'No files found') return rawOutput;
  if (rawOutput.startsWith('Error: ')) return 'Error: glob search failed';

  const lines = rawOutput.split(/\r?\n/);
  const header = lines.shift();
  const match = typeof header === 'string' ? header.match(/^Found ([0-9]+) file\(s\)$/) : null;
  if (match === null || lines.shift() !== '') throw new Error('glob returned an unknown result format');

  const truncation = '(Results are truncated. Consider using a more specific path or pattern.)';
  let wasTruncated = false;
  if (lines.at(-1) === truncation) {
    wasTruncated = true;
    lines.pop();
    if (lines.at(-1) !== '') throw new Error('glob returned a malformed truncation marker');
    lines.pop();
  }
  if (lines.some(line => line.length === 0) || Number(match[1]) !== lines.length) {
    throw new Error('glob returned a mismatched file count');
  }

  const contained = [];
  for (const candidate of lines) {
    const canonical = canonicalizePath(candidate, boundaryRoot);
    if (isContainedPath(boundaryRoot, canonical)) contained.push(candidate);
  }
  if (contained.length === 0) return 'No files found';

  const rendered = [`Found ${contained.length} file(s)`, '', ...contained];
  if (wasTruncated) rendered.push('', truncation);
  return rendered.join('\n');
};

const filesystemPaths = (toolName, args) => {
  const fields = FILESYSTEM_TOOL_PATH_FIELDS.get(toolName.toLowerCase());
  if (!fields) return null;

  const values = [];
  for (const field of fields) {
    const value = args?.[field];
    if (typeof value === 'string' && value.length > 0) {
      values.push(value);
    } else if (Array.isArray(value)) {
      values.push(...value.filter(item => typeof item === 'string' && item.length > 0));
    }
  }
  return values;
};

const canonicalizeFilesystemArgs = (toolName, args, root) => {
  const fields = FILESYSTEM_TOOL_PATH_FIELDS.get(toolName.toLowerCase());
  if (!fields || !args || typeof args !== 'object') return [];

  const normalizedPaths = [];
  for (const field of fields) {
    const value = args[field];
    if (typeof value === 'string' && value.length > 0) {
      const canonical = canonicalizePath(value, root);
      args[field] = canonical;
      normalizedPaths.push({ supplied: value, canonical });
    } else if (Array.isArray(value)) {
      args[field] = value.map(item => {
        if (typeof item !== 'string' || item.length === 0) return item;
        const canonical = canonicalizePath(item, root);
        normalizedPaths.push({ supplied: item, canonical });
        return canonical;
      });
    }
  }
  return normalizedPaths;
};

const canonicalizeContainedPath = (candidate, root, label) => {
  const canonical = canonicalizePath(candidate, root);
  if (!isContainedPath(root, canonical)) {
    throw new Error(`${label} escapes the session directory: ${candidate}`);
  }
  return canonical;
};

const isInSubtree = (root, candidate, ...segments) => {
  // Keep the policy prefix lexical. If qa/changes or qa/archive is itself a
  // symlink, the canonical candidate no longer falls under this path and is
  // rejected rather than laundering another in-session directory through it.
  const subtree = path.resolve(root, ...segments);
  return isContainedPath(subtree, candidate);
};

// Parse one shell command into argv without executing shell syntax. The
// validated argv is always re-emitted with every token single-quoted, so the
// shell cannot reinterpret a path, wildcard, substitution, or operator after
// validation.
const tokenizeBoundedBash = (command) => {
  if (typeof command !== 'string' || command.trim().length === 0) {
    throw new Error('bash command must be a non-empty string');
  }
  if (command.includes('\n') || command.includes('\r') || command.includes('\0')) {
    throw new Error('bash command contains a forbidden control character');
  }

  const tokens = [];
  let token = '';
  let tokenStarted = false;
  let quote = null;
  const forbiddenUnquoted = new Set([';', '&', '|', '<', '>', '(', ')', '$', '`', '\\', '*', '?', '[', ']', '{', '}', '~', '#', '!']);

  for (const character of command) {
    if (quote === "'") {
      if (character === "'") quote = null;
      else token += character;
      continue;
    }
    if (quote === '"') {
      if (character === '"') {
        quote = null;
      } else {
        if (character === '$' || character === '`' || character === '\\') {
          throw new Error('bash command contains expansion inside double quotes');
        }
        token += character;
      }
      continue;
    }
    if (/\s/.test(character)) {
      if (tokenStarted) {
        tokens.push(token);
        token = '';
        tokenStarted = false;
      }
      continue;
    }
    if (character === "'" || character === '"') {
      quote = character;
      tokenStarted = true;
      continue;
    }
    if (forbiddenUnquoted.has(character)) {
      throw new Error(`bash command contains forbidden shell syntax: ${character}`);
    }
    token += character;
    tokenStarted = true;
  }
  if (quote !== null) throw new Error('bash command has an unterminated quote');
  if (tokenStarted) tokens.push(token);
  if (tokens.length === 0) throw new Error('bash command has no argv');
  return tokens;
};

const quoteShellToken = (token) => `'${token.replace(/'/g, `'"'"'`)}'`;

const parseLongOptions = (tokens, start, { values, flags = new Set(), required = new Set() }) => {
  const parsed = new Map();
  for (let index = start; index < tokens.length; index += 1) {
    const option = tokens[index];
    if (!option.startsWith('--') || option.includes('=')) {
      throw new Error(`unexpected positional or combined option: ${option}`);
    }
    if (parsed.has(option)) throw new Error(`duplicate option: ${option}`);
    if (flags.has(option)) {
      parsed.set(option, { optionIndex: index, valueIndex: null, value: true });
      continue;
    }
    if (!values.has(option)) throw new Error(`unsupported option: ${option}`);
    if (index + 1 >= tokens.length || tokens[index + 1].startsWith('--')) {
      throw new Error(`missing value for option: ${option}`);
    }
    index += 1;
    parsed.set(option, { optionIndex: index - 1, valueIndex: index, value: tokens[index] });
  }
  for (const option of required) {
    if (!parsed.has(option)) throw new Error(`missing required option: ${option}`);
  }
  return parsed;
};

const bindPathOption = (tokens, parsed, option, root, { mustEqualRoot = false } = {}) => {
  const item = parsed.get(option);
  if (!item || item.valueIndex === null || typeof item.value !== 'string') return null;
  const canonical = canonicalizeContainedPath(item.value, root, option);
  if (mustEqualRoot && canonical !== root) {
    throw new Error(`${option} must equal the session directory`);
  }
  tokens[item.valueIndex] = canonical;
  return canonical;
};

const validateRiskCommand = (tokens, root) => {
  const subcommand = tokens[2];
  if (subcommand === 'context') {
    const parsed = parseLongOptions(tokens, 3, {
      values: new Set([
        '--change',
        '--project-dir',
        '--diff-base',
        '--archive-depth',
        '--staleness-days',
        '--requirement',
        '--output-dir',
      ]),
      flags: new Set(['--stdout']),
      required: new Set(['--change']),
    });
    bindPathOption(tokens, parsed, '--project-dir', root, { mustEqualRoot: true });
    bindPathOption(tokens, parsed, '--requirement', root);
    const changeID = parsed.get('--change')?.value;
    if (typeof changeID !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(changeID)) {
      throw new Error('unsafe --change value');
    }
    const expectedOutput = path.resolve(root, 'qa', 'changes', changeID, 'explore');
    const suppliedOutput = bindPathOption(tokens, parsed, '--output-dir', root);
    if (suppliedOutput !== null && suppliedOutput !== expectedOutput) {
      throw new Error('--output-dir must equal the declared change explore directory');
    }
    if (suppliedOutput === null && !parsed.has('--stdout')) {
      const canonicalDefault = canonicalizeContainedPath(
        expectedOutput,
        root,
        'default risk output directory',
      );
      if (canonicalDefault !== expectedOutput) {
        throw new Error('default risk output directory crosses a symbolic link');
      }
    }
    return;
  }
  if (subcommand === 'validate-advisory') {
    const parsed = parseLongOptions(tokens, 3, {
      values: new Set(['--change', '--project-dir']),
      required: new Set(['--change']),
    });
    bindPathOption(tokens, parsed, '--project-dir', root, { mustEqualRoot: true });
    return;
  }
  throw new Error(`unsupported aa risk subcommand: ${subcommand ?? '(missing)'}`);
};

const validateAaCommand = (tokens, agent, root) => {
  if (tokens[0] !== 'aa') throw new Error('only the declared aa CLI command is allowed');

  if (agent === 'aa-explorer' || agent === 'aa-intake-host') {
    if (tokens[1] === 'risk') {
      validateRiskCommand(tokens, root);
      return;
    }
  }

  if (agent === 'aa-intake-host' && tokens[1] === 'decide') {
    const parsed = parseLongOptions(tokens, 2, {
      values: new Set(['--change', '--at', '--action', '--reason', '--evidence']),
      required: new Set(['--change', '--at', '--action', '--reason']),
    });
    bindPathOption(tokens, parsed, '--evidence', root);
    return;
  }

  if (agent === 'aa-intake-host' && tokens[1] === 'state' && tokens[2] === 'configure') {
    parseLongOptions(tokens, 3, {
      values: new Set(['--change', '--params-json', '--orchestrator']),
      required: new Set(['--change', '--params-json', '--orchestrator']),
    });
    return;
  }

  if (agent === 'aa-reporter') {
    if (tokens.length === 2 && tokens[1] === '--version') return;
    if (tokens[1] === 'report' && tokens[2] === 'generate') {
      parseLongOptions(tokens, 3, {
        values: new Set(['--change']),
        required: new Set(['--change']),
      });
      return;
    }
  }

  if (agent === 'aa-reviewer') {
    if (tokens.length === 2 && tokens[1] === '--version') return;
    if (tokens[1] === 'report' && tokens[2] === 'inspect') {
      parseLongOptions(tokens, 3, {
        values: new Set(['--change']),
        required: new Set(['--change']),
      });
      return;
    }
  }

  throw new Error(`bash command is not allowed for ${agent}`);
};

const validateArchiveCommand = (tokens, root) => {
  if (tokens[0] === 'mkdir' && tokens[1] === '-p' && tokens.length >= 3) {
    for (let index = 2; index < tokens.length; index += 1) {
      const canonical = canonicalizeContainedPath(tokens[index], root, 'mkdir path');
      if (!isInSubtree(root, canonical, 'qa', 'archive')) {
        throw new Error(`mkdir destination is outside qa/archive: ${tokens[index]}`);
      }
      tokens[index] = canonical;
    }
    return;
  }

  if (tokens[0] === 'cp' && (tokens[1] === '-R' || tokens[1] === '-r') && tokens.length >= 4) {
    for (let index = 2; index < tokens.length - 1; index += 1) {
      const canonical = canonicalizeContainedPath(tokens[index], root, 'cp source');
      if (!isInSubtree(root, canonical, 'qa', 'changes')) {
        throw new Error(`cp source is outside qa/changes: ${tokens[index]}`);
      }
      tokens[index] = canonical;
    }
    const destinationIndex = tokens.length - 1;
    const destination = canonicalizeContainedPath(tokens[destinationIndex], root, 'cp destination');
    if (!isInSubtree(root, destination, 'qa', 'archive')) {
      throw new Error(`cp destination is outside qa/archive: ${tokens[destinationIndex]}`);
    }
    tokens[destinationIndex] = destination;
    return;
  }

  throw new Error('archiver bash is limited to structured mkdir -p and cp -R/-r argv');
};

const constrainBoundedBash = (args, agent, root) => {
  const tokens = tokenizeBoundedBash(args?.command);
  if (agent === 'aa-archiver') validateArchiveCommand(tokens, root);
  else validateAaCommand(tokens, agent, root);
  args.command = tokens.map(quoteShellToken).join(' ');
};

// Simple frontmatter parser (no external dependencies)
const extractAndStripFrontmatter = (content) => {
  const match = content.match(/^---\n([\s\S]*?)\n---\n([\s\S]*)$/);
  if (!match) return { frontmatter: {}, content };

  const frontmatterStr = match[1];
  const body = match[2];
  const frontmatter = {};

  const lines = frontmatterStr.split('\n');
  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    if (/^\s/.test(line)) continue;
    const colonIdx = line.indexOf(':');
    if (colonIdx > 0) {
      const key = line.slice(0, colonIdx).trim();
      const rawValue = line.slice(colonIdx + 1).trim();
      const block = rawValue.match(/^([>|])([+-]?)$/);
      if (block) {
        const blockLines = [];
        while (index + 1 < lines.length && (lines[index + 1].trim() === '' || /^\s/.test(lines[index + 1]))) {
          index += 1;
          blockLines.push(lines[index].replace(/^\s+/, ''));
        }
        let value;
        if (block[1] === '|') {
          value = blockLines.join('\n');
        } else {
          const paragraphs = [];
          let paragraph = [];
          for (const blockLine of blockLines) {
            if (blockLine === '') {
              if (paragraph.length > 0) {
                paragraphs.push(paragraph.join(' '));
                paragraph = [];
              }
            } else {
              paragraph.push(blockLine);
            }
          }
          if (paragraph.length > 0) paragraphs.push(paragraph.join(' '));
          value = paragraphs.join('\n');
        }
        if (block[2] !== '-') value += '\n';
        frontmatter[key] = value;
        continue;
      }
      const value = rawValue.replace(/^["']|["']$/g, '');
      frontmatter[key] = value;
    }
  }

  return { frontmatter, content: body };
};

// Cached bootstrap content (loaded once per session)
let _bootstrapCache = undefined;

const getBootstrapContent = () => {
  if (_bootstrapCache !== undefined) return _bootstrapCache;

  const skillsDir = AA_SKILLS_DIR;
  if (!fs.existsSync(skillsDir)) {
    _bootstrapCache = null;
    return null;
  }

  const skills = [];
  for (const entry of fs.readdirSync(skillsDir)) {
    const skillMdPath = path.join(skillsDir, entry, 'SKILL.md');
    if (!fs.existsSync(skillMdPath)) continue;
    try {
      const raw = fs.readFileSync(skillMdPath, 'utf8');
      const { frontmatter } = extractAndStripFrontmatter(raw);
      if (frontmatter.name && frontmatter.description) {
        skills.push(`- **${frontmatter.name}**: ${frontmatter.description}`);
      }
    } catch {
      // skip unreadable files
    }
  }

  if (skills.length === 0) {
    _bootstrapCache = null;
    return null;
  }

  _bootstrapCache = `
You have AA (Assurance Agent) QA workflow skills available.

Use OpenCode's native \`skill\` tool to load a skill by its actual frontmatter name:
  skill load <skill-name>
For example: \`skill load aa-workflow\` (there is no extra \`aa/\` namespace).

**Available AA Skills:**
${skills.join('\n')}

**Tool Mapping for OpenCode:**
- \`Bash\` / \`Shell\` → Your native bash tool
- \`Read\` / \`Write\` → Your native file tools
- \`TodoWrite\` → \`todowrite\`
- \`Task\` with subagents → OpenCode's subagent system

**Key CLI commands (must be run in terminal, never fabricated):**
- \`aa workflow run --change <id> --entrypoint full|intake|execute|case\` — start GraphRuntime
- \`aa workflow resume --change <id>\` — resume after lease expiry or interruption
- \`aa workflow import-checkpoint --change <id> --manifest <path>\` — validated fixture import
- \`aa status --change <id> --json\` — GraphStatus / pending interrupts
- \`aa run --change <change-id>\` — execute tests (skill: aa-run)
- \`aa report inspect --change <change-id>\` — classify failures (skill: aa-inspect)
`;

  return _bootstrapCache;
};

export default async ({ client, directory }) => {
  const serverRoot = canonicalizePath(directory, directory);

  const loadSessionBoundary = async (sessionID) => {
    try {
      if (typeof client?.session?.get !== 'function') throw new Error('session.get is unavailable');
      const response = await client.session.get({ path: { id: sessionID } });
      const session = response?.data ?? response;
      if (!session || typeof session.directory !== 'string' || typeof session.agent !== 'string') {
        throw new Error('session directory or agent is missing');
      }
      return {
        agent: session.agent,
        root: canonicalizePath(session.directory, session.directory),
      };
    } catch (error) {
      const detail = error instanceof Error ? error.message : String(error);
      throw new Error(`AA sandbox boundary: cannot resolve session root (${detail})`);
    }
  };

  const loadDeclaredSkill = async (sessionID) => {
    try {
      if (typeof client?.session?.messages !== 'function') {
        throw new Error('session.messages is unavailable');
      }
      const response = await client.session.messages({ path: { id: sessionID } });
      const messages = response?.data ?? response;
      if (!Array.isArray(messages)) throw new Error('session messages are malformed');

      for (let messageIndex = messages.length - 1; messageIndex >= 0; messageIndex -= 1) {
        const message = messages[messageIndex];
        if (message?.info?.role !== 'user' || !Array.isArray(message.parts)) continue;
        for (let partIndex = message.parts.length - 1; partIndex >= 0; partIndex -= 1) {
          const part = message.parts[partIndex];
          if (part?.type !== 'text' || typeof part.text !== 'string') continue;
          const match = part.text.match(/\bCall skill\(name=(['"])([^'"]+)\1\)/);
          if (match) return match[2];
        }
      }
      return null;
    } catch (error) {
      const detail = error instanceof Error ? error.message : String(error);
      throw new Error(`AA sandbox boundary: cannot resolve phase-declared skill (${detail})`);
    }
  };

  return {
    // A byte-bound, versioned live marker. The preflight reads the loaded tool
    // catalog, so a refreshed file on disk cannot masquerade as a server that
    // is still executing an older boundary plugin.
    tool: {
      [AA_BOUNDARY_PROBE_TOOL]: {
        description: 'AA boundary plugin live-byte marker (preflight only)',
        args: {},
        async execute() {
          return AA_BOUNDARY_PLUGIN_SHA256;
        },
      },
    },

    // Register AA skills directory for native OpenCode discovery.
    // Skip when oh-my-openagent (OMO) is installed: OMO replaces the native
    // skill tool. `aa skill refresh --sync-agents` supplies OMO's project-native
    // .opencode/skills mirror, so registering skills.paths would duplicate every
    // aa-* skill in the palette.
    config: async (config) => {
      const plugins = Array.isArray(config.plugin) ? config.plugin : [];
      const usesOmo = plugins.some((p) => /oh-my-openagent|oh-my-opencode/i.test(String(p)));
      if (usesOmo) return;

      config.skills = config.skills || {};
      config.skills.paths = config.skills.paths || [];
      if (!config.skills.paths.includes(AA_SKILLS_DIR)) {
        config.skills.paths.push(AA_SKILLS_DIR);
      }
    },

    // Inject brief bootstrap context into the first user message of each session
    'experimental.chat.messages.transform': async (_input, output) => {
      const bootstrap = getBootstrapContent();
      if (!bootstrap || !output.messages.length) return;

      const firstUser = output.messages.find(m => m.info.role === 'user');
      if (!firstUser || !firstUser.parts.length) return;

      // Guard: skip if already injected
      if (firstUser.parts.some(p => p.type === 'text' && p.text.includes('AA (Assurance Agent)'))) return;

      const ref = firstUser.parts[0];
      firstUser.parts.unshift({ ...ref, type: 'text', text: bootstrap });
    },

    'tool.execute.before': async (input, output) => {
      const toolName = input.tool.toLowerCase();
      const boundary = await loadSessionBoundary(input.sessionID);
      if (!AA_BOUNDED_AGENTS.has(boundary.agent)) return;

      if (!BOUNDED_AGENT_ALLOWED_TOOLS.has(toolName)) {
        throw new Error(`AA sandbox boundary: ${input.tool} is unavailable to bounded AA agents`);
      }

      if (toolName === 'bash') {
        try {
          constrainBoundedBash(output.args, boundary.agent, boundary.root);
        } catch (error) {
          const detail = error instanceof Error ? error.message : String(error);
          throw new Error(`AA sandbox boundary: invalid bash command (${detail})`);
        }
        return;
      }

      const paths = filesystemPaths(toolName, output.args);
      const loadsSkill = toolName === 'skill';
      const startsWorkflow = toolName === 'workflow_start';

      if (paths !== null && paths.length === 0) {
        const rootField = IMPLICIT_SEARCH_ROOT_FIELDS.get(toolName);
        if (rootField !== undefined) {
          output.args ??= {};
          output.args[rootField] = rootField === 'paths' ? [boundary.root] : boundary.root;
          paths.push(boundary.root);
        }
      }

      if (startsWorkflow && boundary.agent !== 'aa-intake-host') {
        throw new Error(`AA sandbox boundary: ${input.tool} is unavailable to bounded phase agents`);
      }

      if (loadsSkill) {
        const declaredSkill = await loadDeclaredSkill(input.sessionID);
        const requestedSkill = output.args?.name;
        if (declaredSkill === null) {
          if (boundary.agent !== 'aa-intake-host' || !INTAKE_HOST_SKILLS.has(requestedSkill)) {
            throw new Error('AA sandbox boundary: phase worker prompt has no phase-declared skill');
          }
        }
        if (declaredSkill !== null && requestedSkill !== declaredSkill) {
          throw new Error(
            `AA sandbox boundary: ${requestedSkill} is not the phase-declared skill ${declaredSkill}`,
          );
        }
      }

      if (paths === null) return;
      let normalizedPaths;
      try {
        normalizedPaths = canonicalizeFilesystemArgs(toolName, output.args, boundary.root);
      } catch (error) {
        const detail = error instanceof Error ? error.message : String(error);
        throw new Error(`AA sandbox boundary: cannot resolve ${input.tool} path (${detail})`);
      }
      for (const { supplied, canonical } of normalizedPaths) {
        if (!isContainedPath(boundary.root, canonical)) {
          throw new Error(
            `AA sandbox boundary: ${input.tool} path escapes the session directory: ${supplied}`,
          );
        }
      }

      if (toolName === 'glob') {
        const searchRoots = normalizedPaths.map(item => item.canonical);
        for (const canonicalRoot of searchRoots) {
          try {
            assertGlobCannotFollowEscapingSymlink(canonicalRoot, boundary.root, serverRoot);
          } catch (error) {
            const detail = error instanceof Error ? error.message : String(error);
            throw new Error(`AA sandbox boundary: glob symlink audit failed (${detail})`);
          }
        }
      }
    },

    'tool.execute.after': async (input, output) => {
      if (input.tool.toLowerCase() !== 'glob') return;
      const boundary = await loadSessionBoundary(input.sessionID);
      if (!AA_BOUNDED_AGENTS.has(boundary.agent)) return;
      try {
        output.output = filterGlobOutputToSession(output.output, boundary.root);
      } catch (error) {
        const detail = error instanceof Error ? error.message : String(error);
        throw new Error(`AA sandbox boundary: cannot validate glob result (${detail})`);
      }
    },
  };
};
