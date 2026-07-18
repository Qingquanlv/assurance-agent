// assurance_agent/_resources/opencode/tools/workflow_start.ts
/**
 * OpenCode custom tool: start the AA workflow driver detached from chat.
 * Synced into a project's .opencode/tools/ by `aa init` / `aa skill refresh --sync-agents`.
 *
 * This tool ONLY collects params and delegates to
 * `aa workflow run --detach --adapter opencode` (M6 detached entry).
 * The dispatch loop lives in the Python driver, never in JS.
 *
 * Fail-closed server URL: requires AA_OPENCODE_SERVER_URL (or OPENCODE_SERVER_URL),
 * or explicit --hostname/--port on the opencode process argv (no implicit port).
 */
import { spawnSync } from 'child_process';
import { z } from 'zod';

function resolveServerUrl(): string {
  const url = process.env.AA_OPENCODE_SERVER_URL || process.env.OPENCODE_SERVER_URL;
  if (url && url.trim()) return url.trim().replace(/\/$/, '');
  const argv = process.argv;
  let hostname: string | null = null;
  let port: string | null = null;
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if ((a === '--hostname' || a === '--host') && argv[i + 1]) hostname = argv[i + 1];
    if (a === '--port' && argv[i + 1]) port = argv[i + 1];
    const hm = /^--hostname=(.+)$/.exec(a) || /^--host=(.+)$/.exec(a);
    if (hm) hostname = hm[1];
    const pm = /^--port=(.+)$/.exec(a);
    if (pm) port = pm[1];
  }
  if (hostname && port) {
    const host = hostname === '0.0.0.0' ? '127.0.0.1' : hostname;
    return `http://${host}:${port}`;
  }
  throw new Error(
    'Cannot resolve OpenCode server URL. Set AA_OPENCODE_SERVER_URL ' +
    '(or OPENCODE_SERVER_URL), or start opencode with explicit --hostname/--port.',
  );
}

export default {
  description:
    'Start or continue the AA workflow driver for a change in the background. ' +
    'Call after intake user confirmation, or after a human decision. ' +
    'Progress appears in nested sessions and the QA panel.',
  args: {
    change_id: z.string().regex(
      /^[A-Za-z0-9][A-Za-z0-9._-]*$/,
      'change_id must be one safe path segment',
    ).describe('Change ID (e.g. REQ-001-login)'),
    scope: z.enum(['execute', 'full']).default('execute').describe('Workflow scope'),
    params_json: z.string().optional().describe('Optional runtime params as JSON object string'),
  },
  async execute(
    args: { change_id: string; scope?: 'execute' | 'full'; params_json?: string },
    context: { sessionID?: string; sessionId?: string; directory?: string; worktree?: string },
  ) {
    const scope = args.scope ?? 'execute';
    const directory = context.directory ?? context.worktree ?? process.cwd();
    const parent = context.sessionID ?? context.sessionId;

    let serverUrl: string;
    try {
      serverUrl = resolveServerUrl();
    } catch (err) {
      return `workflow_start failed: ${(err as Error).message}`;
    }

    const cliArgs = [
      'workflow', 'run',
      '--detach',
      '--adapter', 'opencode',
      '--change', args.change_id,
      '--scope', scope,
      '--server', serverUrl,
      '--directory', directory,
    ];
    if (parent) cliArgs.push('--parent-session', parent);
    if (args.params_json) cliArgs.push('--params', args.params_json);

    const result = spawnSync('aa', cliArgs, {
      cwd: directory,
      encoding: 'utf-8',
      env: process.env,
    });
    const out = `${result.stdout ?? ''}${result.stderr ?? ''}`.trim();
    if (result.error) {
      return `workflow_start failed: ${result.error.message}${out ? `\n${out}` : ''}`;
    }
    if (result.status !== 0) {
      return `workflow_start failed (exit ${result.status}): ${out || 'no output'}`;
    }
    return out || `已启动 workflow（change=${args.change_id}, scope=${scope}）`;
  },
};
