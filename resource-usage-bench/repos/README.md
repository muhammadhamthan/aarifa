# Third-party source used by the bench

Cloned so the bench runs and builds the real upstream code at the exact versions the platform uses. These
folders are ignored by git (`.gitignore` here): they are not part of the Kairox platform and must not be committed.

| Folder | Upstream | Version | Why this version | Used for |
|---|---|---|---|---|
| `livekit-server/` | https://github.com/livekit/livekit | tag `v1.13.11` | the server version the bench ran (`livekit/livekit-server:latest` reported 1.13.11 on 14 Nov 2026) | built from source (`docker build -t kxbench/livekit-server:v1.13.11-src livekit-server`) and run in Round 2b |
| `livekit-agents/` | https://github.com/livekit/agents | tag `livekit-agents@1.8.10` | the platform pins `livekit-agents==1.8.10` (`uv.lock`) | `livekit-plugins-openai` and `livekit-plugins-silero` 1.8.10 built from this source into `F:/kx-bench/pylib/appvoice` for the platform's real voice worker |

Clone again with:

```bash
git clone --depth 1 --branch v1.13.11 https://github.com/livekit/livekit.git livekit-server
git clone --depth 1 --branch "livekit-agents@1.8.10" https://github.com/livekit/agents.git livekit-agents
git -C livekit-agents config core.longpaths true   # Windows: the project path is long
git -C livekit-agents restore --source=HEAD :/
```

Every other platform the bench tested ran from its official image or official compose file. Its source is cloned
here too (shallow, at the measured version where that tag exists) by `../scripts/clone_repos.sh`. The list, with
the ref and commit cloned and each licence file's first line, is in **`SOURCES.md`**. The compose files that
started SafeLine, Langfuse, Opik and Postal are in the copies on `F:/kx-bench/repos`.
