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
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

// Skills live two levels up from .opencode/plugins/
const AA_SKILLS_DIR = path.resolve(__dirname, '../../skills');

// Simple frontmatter parser (no external dependencies)
const extractAndStripFrontmatter = (content) => {
  const match = content.match(/^---\n([\s\S]*?)\n---\n([\s\S]*)$/);
  if (!match) return { frontmatter: {}, content };

  const frontmatterStr = match[1];
  const body = match[2];
  const frontmatter = {};

  for (const line of frontmatterStr.split('\n')) {
    const colonIdx = line.indexOf(':');
    if (colonIdx > 0) {
      const key = line.slice(0, colonIdx).trim();
      const value = line.slice(colonIdx + 1).trim().replace(/^["']|["']$/g, '');
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
- \`aa workflow status --change <id> --json\` — graph status / pending interrupts
- \`aa workflow resume --change <id>\` — resume after lease expiry or interruption
- \`aa workflow import-checkpoint --change <id> --manifest <path>\` — validated fixture import
- \`aa status --change <change-id> --json\` — GraphStatus projection
- \`aa run --change <change-id>\` — execute tests (skill: aa-run)
- \`aa report inspect --change <change-id>\` — classify failures (skill: aa-inspect)
`;

  return _bootstrapCache;
};

export default async ({ client, directory }) => {
  return {
    // Register AA skills directory for native OpenCode discovery.
    // Skip when oh-my-openagent (omo) is installed: omo replaces the native skill
    // tool and only scans ~/.config/opencode/skills/. Registering skills.paths as
    // well would duplicate every aa-* skill in the palette.
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
  };
};
