# Installation and updates

Install the standalone `find` skill with the Skills CLI, or use the `skill` plugin in Claude Code or Codex. Choose one route per agent to avoid duplicate skills. Install from GitHub or a local checkout. Versioned releases are tracked in [CHANGELOG.md](../CHANGELOG.md).

## Prerequisites

Use a coding agent that supports skills and can run local commands. Universal Skill Finder requires **Python 3.10+ with working SSL**, with no third-party Python packages. Skills CLI installation also requires **Node.js 22.20.0+**, `npx`, and Git. Native plugins and manual copying do not require Node.js.

Before running the finder, its launcher checks Python and SSL. A missing or unsupported runtime produces a concise error before configuration, cache, or network access. Make a supported interpreter available to the agent, then retry. This check runs when the skill is used; successful installation alone does not verify Python.

## Skills CLI: any supported coding agent

From GitHub:

```bash
npx skills@1.5.23 add bibryam/universal-skill-finder --skill find --global
```

Or from the root of a local checkout:

```bash
npx skills@1.5.23 add . --skill find --global
```

The installer lets you choose which agents receive the skill. `--global` makes it available across projects; omit it for a project installation. To choose an agent directly, append the corresponding option:

| Agent | Option |
|---|---|
| Claude Code | `--agent claude-code` |
| Codex | `--agent codex` |
| Cursor | `--agent cursor` |
| GitHub Copilot | `--agent github-copilot` |
| Gemini CLI | `--agent gemini-cli` |
| OpenCode | `--agent opencode` |
| All supported agents | `--agent '*'` |

These commands work in POSIX shells and PowerShell. Add `--copy` if you prefer independent copies or your system cannot create symlinks. The installer copies the engine and supporting files with the skill; no separate engine setup is required.

Start a new agent session. Ask “Use Universal Skill Finder to find skills for PDF forms,” invoke `/find pdf forms` in Claude Code or `$find pdf forms` in Codex, or use your agent's skill picker. Other hosts can search and inspect repository links; generated installation commands and installed-skill checks currently support Codex and Claude Code only.

[Skills CLI 1.5.23 documentation](https://github.com/vercel-labs/skills/tree/v1.5.23#install-a-skill) covers additional agents and options. The pinned version sends anonymous usage telemetry and requests security-audit data for confirmed-public GitHub repositories by default. Set `DISABLE_TELEMETRY=1` or `DO_NOT_TRACK=1` to disable both. Universal Skill Finder's engine has no telemetry.

## Manual copy: without Node.js

Copy the **entire** [`skills/find`](../skills/find) directory to your agent's skill directory. `SKILL.md` alone will not work: the skill needs its bundled scripts, references, configuration, and license. Use an agent that can run the Python engine; uploading just the Markdown file to a chat application is insufficient.

For a project-level Codex installation, from this checkout on macOS/Linux:

```bash
mkdir -p .agents/skills
cp -R skills/find .agents/skills/find
```

In PowerShell:

```powershell
New-Item -ItemType Directory -Force .agents/skills | Out-Null
Copy-Item -Recurse skills/find .agents/skills/find
```

For Claude Code use `.claude/skills/find` instead. For another agent, use its documented skill directory. The commands assume the destination `find` folder does not already exist; inspect an existing installation before replacing it. For a different project, use that project's destination path. Start a new session afterward.

## Native plugins from GitHub

### Claude Code

```bash
claude plugin marketplace add bibryam/universal-skill-finder
claude plugin install skill@skill
```

Start a new session or run `/reload-plugins`. Ask: “Use Universal Skill Finder to find skills for PDF forms.” You can also invoke `/skill:find` followed by your request.

### Codex

```bash
codex plugin marketplace add https://github.com/bibryam/universal-skill-finder.git
codex plugin add skill@skill
```

Start a new task. Ask: “Use Universal Skill Finder to find skills for PDF forms,” invoke `$skill:find pdf forms`, or select it from the skill picker.

## Native plugins from a local directory

Open a terminal in this repository's root directory, which contains both marketplace manifests. No separate engine installation is needed.

For Claude Code:

```bash
claude plugin marketplace add ./
claude plugin install skill@skill
```

For Codex:

```bash
codex plugin marketplace add .
codex plugin add skill@skill
```

Start a new task/session afterward. Claude's local-directory argument is `./`, not bare `.`. Keep the local checkout available for future plugin updates.

Use only one `skill` marketplace location per client. To switch between a GitHub installation and a local directory, remove the installed plugin, then remove the old marketplace registration before adding the new location:

```bash
claude plugin uninstall skill@skill
claude plugin marketplace remove skill
```

Or, in Codex:

```bash
codex plugin remove skill@skill
codex plugin marketplace remove skill
```

Then use the installation commands for your chosen location above.

## Choose sources

No setup is required to search the enabled defaults. Ask **List sources** to see each source’s type, current enabled state, requirements, and toggle request. Ask **Disable skillsmp** or **Enable tessl** to persist a choice.

For direct editing, ask **Create source config**. This writes the complete list of known sources and current choices to one file, `~/.config/universal-skill-finder/sources.json` for a new installation. Change an entry's `enabled` value to `true` or `false`:

```json
{
  "sources": [
    {"id": "skills-sh", "enabled": true},
    {"id": "skillsmp", "enabled": false},
    {"id": "tessl", "enabled": true}
  ]
}
```

This shortened example changes only the listed IDs; other sources keep their defaults. **Show source config** reports the selected file without creating it. Listing and searching never rewrite it. Explicit paths, `$XDG_CONFIG_HOME`, and `UNIVERSAL_SKILL_FINDER_CONFIG` can change the location. Advanced disabled packs remain a separate blocker; enabling a source does not silently enable its pack. See [configuration](../skills/find/references/configuration.md).

## Update

For a Skills CLI installation from GitHub:

```bash
npx skills@1.5.23 update find --global
```

Use `--project` instead of `--global` for project scope. The updater skips local sources: after updating a local checkout, rerun the original `npx skills@1.5.23 add . --skill find --global` command with the same scope and agent selection. Manual copies must be replaced with the complete updated `skills/find` folder.


For a Git-backed Codex marketplace:

```bash
codex plugin marketplace upgrade skill
codex plugin add skill@skill
```

For Claude Code:

```bash
claude plugin marketplace update skill
claude plugin update skill@skill
```

Start a new task/session afterward. For a local marketplace, first update the checkout, then reinstall the plugin from that directory. Codex's marketplace upgrade refreshes Git-backed marketplaces only. Plugin releases must bump their version so cached installations receive changes.

## Remove

For a Skills CLI installation:

```bash
npx skills@1.5.23 remove find --global
```

Omit `--global` for project scope. Add the same `--agent` selector used during installation if removing it from one agent only. For a manual copy, remove only the `find` folder you installed.


For Codex:

```bash
codex plugin remove skill@skill
```

For Claude Code:

```bash
claude plugin uninstall skill@skill
```

Removing the skill or plugin does not delete the finder's configuration or query cache. Before uninstalling, ask your assistant to show those paths if you also want to remove that data. Remove it separately only if you no longer want its contents.

Official client references: [Codex marketplace commands](https://learn.chatgpt.com/docs/developer-commands#codex-plugin-marketplace), [Claude plugin installation](https://code.claude.com/docs/en/discover-plugins), [Claude skill namespacing](https://code.claude.com/docs/en/plugins).
