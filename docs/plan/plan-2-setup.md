# Plan 2 — `setup` コマンド (依存解決 / auth 確立 / 修復)

> [!NOTE]
> Historical design record. It may describe pre-implementation state, internal
> plan numbers, or behavior that has since changed. See the
> [current README](../../README.md) and [document map](../README.md).

**Status:** 設計 (implementation not started)  
**Date:** 2026-06-06  
**Author:** kagura-engineer dev  
**Depends on:** Plan 1 (doctor) — done at HEAD `657bb1b`; hardened with `c29f939` + `63b9155`

---

## 0. 前提 (Plan 1 doctor の確定状態)

`kagura-engineer doctor` は 6 check で FAIL/WARN/OK を返す:

| check | OK 条件 | FAIL fix_hint |
|---|---|---|
| `git` | `git rev-parse --is-inside-work-tree` が true | `kagura-engineer setup --fix git` |
| `claude-code` | `claude --version` 動作 + auth あり (API key or subscription login) | `kagura-engineer setup --fix claude-code` |
| `gh` | `gh auth status` 終了コード 0 | `gh auth login` / `--fix gh` |
| `ollama` | daemon 到達 + `cfg.review.models` 全存在 | `ollama serve` / `ollama pull <name>` |
| `haiku` | API key あり OR Claude Code credential cache (`~/.claude/.credentials.json` or `~/.claude.json`) あり | (WARN のみ、空 env は FAIL) |
| `memory-cloud` | `{base_url}/health` HTTP 応答あり (4xx = WARN, 5xx/unreachable = FAIL) | `check config.memory_cloud_url / network` |

**サブスクモード基本**: 本プロジェクトは `ANTHROPIC_API_KEY` を使わず、Claude Code の OAuth subscription login を前提とする。`check_haiku` は `~/.claude/.credentials.json` の存在で OK 判定する(Plan 1 commit `63b9155` で実装済)。

`fix_hint` 文字列が Plan 2 の `--fix <name>` 許可語彙と一致 → vocabulary をそのまま再利用する (zero friction)。

---

## 1. 設計の全体像

### 1.1 CLI surface

```python
@app.command()
def setup(
    config: str = _CONFIG_OPT,                                 # 既存
    fix: str | None = typer.Option(None, "--fix", help="..."), # 単一 step filter
    no_input: bool = typer.Option(False, "--no-input", help="never prompt; fail loudly on user-action steps"),
    unsafe_auto_install: bool = typer.Option(False, "--unsafe-auto-install", help="reserved for future use; current default already auto-installs with sudo"),
    dry_run: bool = typer.Option(False, "--dry-run", help="preview all steps without executing; exit 0 if plan is feasible, 1 otherwise"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
```

許可 `--fix <name>` 語彙(doctor の check name と一致):
`{git, claude-code, gh, ollama, ollama-models, haiku, memory-cloud}` — 計 7 step。

### 1.2 Module layout

```
src/kagura_engineer/setup/
    __init__.py         # orchestrator: build_plan(), run_plan(), SetupReport
    platform.py         # detect_os(), detect_pkg_manager(), is_wsl()
    git.py              # install_git(), ensure_git()
    claude.py           # install_claude(), ensure_claude_login()
    gh.py               # install_gh(), ensure_gh_auth()
    ollama.py           # install_ollama(), ensure_ollama_up(), pull_models()
    memory_cloud.py     # ensure_memory_cloud_reachable() (auth deferred to Plan 3)
    result.py           # StepResult, StepStatus (mirrors doctor/result.py: Status + 'skipped','needs_user')
    render.py           # print_report()
```

### 1.3 Data model

```python
class StepStatus(str, Enum):
    OK = "ok"             # already healthy or auto-fixed
    SKIPPED = "skipped"   # already done
    NEEDS_USER = "needs_user"  # requires interactive input or env var
    FAIL = "fail"         # hard error

class StepResult(BaseModel):
    name: str
    status: StepStatus
    detail: str
    fix_hint: str | None = None
    duration_s: float = 0.0

class SetupReport(BaseModel):
    ran: list[StepResult]
    skipped: list[StepResult]
    failed: list[StepResult]
    needs_user: list[StepResult]
    duration_s: float
```

### 1.4 Orchestrator

```python
def build_plan(cfg: Config, *, only: str | None = None) -> list[Step]:
    all_steps = [
        ensure_git, ensure_claude_login, ensure_gh_auth,
        ensure_ollama_up, pull_ollama_models,
        ensure_memory_cloud_reachable,
    ]
    if only:
        all_steps = [s for s in all_steps if s.name == only]
    return all_steps

def run_plan(steps: list[Step], *, no_input: bool, dry_run: bool = False) -> SetupReport:
    ...
```

**Idempotency**: 各 step は「check first, install only if missing, treat already-installed as success」(`brew bundle` パターン)。re-run 可能な drift repair。

### 1.5 動作モード

| Mode | Behavior |
|---|---|
| `setup` (no flag) | full provision: doctor 走査 → FAIL/WARN step を順次実行 → 対話 login 必要なら prompt |
| `setup --fix <name>` | doctor 走査 skip、該当 step のみ |
| `setup --no-input` | 対話 prompt 一切しない、user-action step は即 FAIL loud |
| `setup --json` | `SetupReport` を JSON で stdout |

### 1.6 Exit code

| code | 意味 |
|---|---|
| 0 | 全 step OK or SKIPPED |
| 1 | 1+ step FAIL (hard error) |
| 2 | 1+ step NEEDS_USER (interactive 必要) — `--no-input` で non-zero 確実化 |

→ `#10` review 指摘 (stub の code=2 衝突) は Plan 2 実装で **stub 削除 + 上記 contract に置換** で解決。

---

## 2. step 別の動作

### 2.1 `ensure_git`

**User spec call Q2.1: `sudo` を default で自動実行する** (recommended の "print-only" ではなく user の判断を採用)。理由として user 環境を想定:

- 個人の dev container / VM / クラウド instance を前提 (共有ホストではない)
- setup は **明示的に user が起動する** コマンドなので暗黙の privileged op ではない
- print-only は "親切だが二度手間" になりがち

→ default で `sudo` 自動実行。`--dry-run` flag で全 step を preview のみに留める escape hatch を用意する (`--unsafe-auto-install` は reserved として未使用)。

| State | 動作 |
|---|---|
| `git` on PATH | SKIPPED |
| 無い + default | `sudo apt install -y git` (or brew/dnf/pacman/winget) → 5s timeout で完了確認 |
| 無い + `--dry-run` | print のみ → NEEDS_USER 扱いにはしない (dry-run は preview 専用) |
| `sudo` パスワード待ちで timeout | NEEDS_USER: "passwordless sudo required, or run with --no-input + pre-authorize" |

### 2.2 `ensure_claude_login`

| State | 動作 |
|---|---|
| `claude --version` exit 0 + `~/.claude/.credentials.json` 存在 | OK (subscription login 済) |
| `claude --version` exit 0 + cache 不在 | NEEDS_USER: "run `claude` once interactively" |
| `claude` not on PATH + `--unsafe-auto-install` | `curl -fsSL https://claude.ai/install.sh \| bash` |
| `claude` not on PATH + default | NEEDS_USER: install hint |
| 環境変数 `ANTHROPIC_API_KEY=""` | FAIL (これは設定ミス、Plan 1 commit `c29f939` で doctor が検出する) |

### 2.3 `ensure_gh_auth`

| State | 動作 |
|---|---|
| `gh auth status` exit 0 | OK |
| `GITHUB_TOKEN` / `GH_TOKEN` env あり | OK (token-passthrough 使える) |
| 上記以外 | NEEDS_USER: "run `gh auth login` (browser flow)" |

### 2.4 `ensure_ollama_up`

| State | 動作 |
|---|---|
| `GET {ollama_url}/api/tags` 2xx | OK |
| daemon 未起動 | `ollama serve &` を試行 → 5s wait → 再 probe |
| 起動失敗 or binary 不在 | NEEDS_USER: install/serve hint |

### 2.5 `pull_ollama_models`

| State | 動作 |
|---|---|
| `cfg.review.models` 空 | SKIPPED |
| 全 model が `/api/tags` に存在 | OK (idempotent) |
| 欠落あり | `ollama pull <name>` (各 model 逐次, `--no-input` なら即実行) |
| pull 失敗 (network/disk) | FAIL |

### 2.6 `ensure_memory_cloud_reachable`

Plan 1 doctor の `check_memory_cloud` を呼ぶだけ。auth 検証は Plan 3 recall smoke に defer(README / Plan 1 commit `6d2894c` のコメントと整合)。

| State | 動作 |
|---|---|
| `{base_url}/health` 2xx | OK |
| 4xx (host up, endpoint auth'd) | OK (WARN として report) |
| unreachable / 5xx | FAIL |

---

## 3. Plan 3 / Plan 4 / Plan 5 との接続

### 3.1 `run` (Plan 3) は `setup --no-input` を guard として先叩く

```python
# pipeline.py (sketch)
def run_idea_mode(cfg: Config, task: str) -> RunResult:
    setup_report = run_setup(cfg, no_input=True, unsafe=False)
    if setup_report.failed or setup_report.needs_user:
        raise SetupBlocked(setup_report)
    # ... rest of pipeline
```

**Contract**: "setup is the spec for run" — Plan 3 は doctor/setup の健全性を仮定する。

### 3.2 `review` (Plan 4) の fix backend 判定

`fix_backend: "auto" | "claude" | "ollama" | "human"` のうち `"auto"` の解決:

```python
def resolve_fix_backend(cfg: ReviewConfig) -> Literal["claude", "ollama"]:
    if cfg.fix_backend in ("claude", "ollama"):
        return cfg.fix_backend
    # auto: API key or credential cache → claude; otherwise ollama
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "claude"
    cred = Path.home() / ".claude" / ".credentials.json"
    legacy = Path.home() / ".claude.json"
    if cred.exists() or legacy.exists():
        return "claude"
    return "ollama"
```

Plan 1 commit `63b9155` で実装した credential cache 検出ロジックをここでも再利用する(共有 helper に切り出す予定)。

### 3.3 `memory` (Plan 5) は独立

`KAGURA_API_KEY` 別系統、setup は触らない (memory cloud 自体は reachability 確認のみ、auth は Plan 3 recall smoke で)。

---

## 4. Open Questions (Plan 2 関連分のみ — user spec call 必要)

| # | Question | 推奨 default | 影響 |
|---|----------|------------|------|
| **Q2.1** | `sudo apt install` の自動実行 default? | **print-only** + `--unsafe-auto-install` opt-in | セキュリティ / 利便性トレードオフ |
| **Q2.2** | `gh` auth no-TTY + token 無し | **fail loudly** with `export GITHUB_TOKEN` hint | CI / container 親和性 |
| **Q2.3** | Ollama daemon 未起動 | **`ollama serve &` 試行** + 5s wait + fall back to instruction | macOS .app で頻発 |
| **Q2.4** | setup の re-runnability | **re-runnable** (drift repair 兼ねる) | step 設計の冪等性要求 |
| **Q2.5** | `--fix <name>` 許可語彙 | **`{git, claude-code, gh, ollama, ollama-models, haiku, memory-cloud}`** (7 個) | doctor 語彙との 1:1 |
| **Q2.6** | `ANTHROPIC_API_KEY` probe (`GET /v1/models`) | **不要** (サブスクモード基本) → `claude -p "ping"` 5s probe に置換 | ネットワーク要件 |

(Open Q 全体一覧は Section 6 参照)

---

## 5. 実装の順序 (Plan 2 着手時の推奨)

**Task 0 (config 拡張)** — Q1 spec call 反映:
- `src/kagura_engineer/config.py` に `workspace_id: str` 追加
- `test_config.py` 拡張 (必須フィールド化)
- `tests/test_cli.py` の `_write_cfg` fixture にも `workspace_id` 追加
- 1 commit ("feat(config): add workspace_id for Memory Cloud filter hierarchy")

**Task 1-10 (setup 実装)**:
1. `src/kagura_engineer/setup/` scaffold (`__init__.py` + `result.py` + `render.py`)
2. `platform.py` (OS / pkg manager detection、WSL flag)
3. `git.py` (最小の `shutil.which` + sudo pkg install パターン、後で他 step も真似る)
4. `claude.py` (credential cache 検出ロジックを Plan 1 と共有 helper `resolve_anthropic_auth()` に切り出し)
5. `gh.py` / `ollama.py` / `memory_cloud.py`
6. `__init__.py` orchestrator + `build_plan` + `run_plan` (`--dry-run` 対応)
7. CLI 配線 (`cli.py` の `setup` stub 削除 + 新 command 実装、`#10` review 指摘の exit code contract も同時に解決)
8. Tests: 各 step 個別 (`monkeypatch` で `shutil.which` / `subprocess.run` / `Path.home` / `urllib.request.urlopen` mock)、orchestrator は Plan 1 と同パターン
9. `--json` output test、`--dry-run` test、`--no-input` test
10. Plan 3 (run) からの `setup --no-input` 接続点 placeholder (Plan 2 内ではダミー呼び出し)

---

## 6. Open Questions — 確定状況 (2026-06-06 spec call 反映)

### 6.1 確定 (Plan 2 実装着手可)

| # | Question | 確定回答 | 設計書反映箇所 |
|---|----------|---------|--------------|
| **Q1** | `workspace_id` を `Config` に追加? | **YES** — Memory Cloud の `workspace_id → context_id → user_id` 階層に必要 | Section 8 で config 拡張として明記、Plan 2 step 着手時に同時投入 |
| **Q2** | Auth 解決順 | **env → keyring → Config フィールド** | 共有 helper `resolve_token(env_var, config_field)` を Plan 2 で新設、Plan 3/5 でも利用 |
| **Q2.1** | `sudo` auto-install default | **`sudo` 自動 default** (recommended ではない、user 環境想定) | Section 2.1 全面書換、`--dry-run` flag 追加 |
| **Q2.2** | `gh` auth no-TTY | **fail loudly** with `export GITHUB_TOKEN` hint | Section 2.3 通り |
| **Q2.3** | Ollama daemon 未起動 | **`ollama serve &` 試行 → 5s wait → fall back** | Section 2.4 通り |
| **Q2.4** | setup の re-runnability | **re-runnable** (drift repair 兼ねる) | 冪等性要求を Section 1.4 に明記 |
| **Q2.5** | `--fix <name>` 許可語彙 | **{git, claude-code, gh, ollama, ollama-models, haiku, memory-cloud}** (7 個) | Section 1.1 通り |
| **Q2.6** | `ANTHROPIC_API_KEY` probe (`GET /v1/models`) | **不要** (サブスクモード基本) → `claude -p "ping"` 5s probe に置換 | Section 2.2 通り |
| **Q4** | Plan 4 review gate default | **hard gate** + `--allow-exhausted` + `--interactive` | Plan 4 着手時に反映、Plan 2 では無関係 |
| **Q6** | Plan 5 memory auto-store | **auto on** + `--no-remember` opt-out | Plan 5 着手時に反映、Plan 2 では無関係 |

### 6.2 未確定 (他 plan 着手時に再 spec call)

| # | Plan | Question | 推奨 default | spec call タイミング |
|---|------|----------|------------|---------------------|
| Q3 | 3 | `claude -p` 起動戦略 (subprocess 直 vs Agent SDK) | **subprocess 直** (依存最小) | Plan 3 着手時 |
| Q5 | 3 | `context_id` 寿命 (per-project / per-run) | **per-project** + per-run sub-context | Plan 3 着手時 |
| Q7 | 5 | Offline mode (LocalMemoryClient SQLite 用意?) | **YES** (Protocol 2nd impl) | Plan 5 着手時 |
| Q8 | 5 | Memory privacy (`trust_tier` filter) | **kagura-engineer 産出は `trusted`** | Plan 5 着手時 |
| Q9 | 4 | `cfg.review.models` 空時の挙動 | **`run` で review skip** | Plan 4 着手時 |
| Q10 | 5 | Workspace bootstrap (`create_context`) を setup に含める? | **YES** (Plan 2 step として) | Plan 2 着手時 mini spec call |
| Q11 | 3 | Run 単位 worktree 命名 | **`run-<short-id>`** (default) | Plan 3 着手時 |
| Q12 | 3 | Memory recall budget | **2k tokens pinned + 4k on-demand `recall()`** | Plan 3 着手時 |

### 6.3 Plan 2 実装着手時の mini spec call (Q10 のみ)

Q10: `create_context` を setup に含めるか?

- YES 推奨: setup で workspace bootstrap まで済ませる = "one-shot make work" 体験
- NO: setup は reachability 確認のみ、context 作成は `kagura-engineer memories init` 別コマンド

→ Plan 2 実装中に default YES で進め、Plan 5 着手時に正式 spec call の形で再確認すれば十分。

---

## 7. 実装メモ (research レポートからの持ち越し)

- Ollama API structured output: `POST /api/generate` + `format: <json-schema>` (`num_ctx=8192`, `temperature=0.0`, `stream:false`)
- Review verdict Pydantic schema: `ReviewComment(severity, category, file, line, message)` + `ReviewVerdict(pass_, comments, raw)` + `RoundVerdict(round, per_model, all_pass)`
- 依存追加予定: `instructor>=1.0` (Ollama provider), `httpx` (async), `pyjwt` (credential cache 検証、Plan 4 で使うかも)
- 5 category review (series): spec → correctness → test → style → security
- Unanimous-AND ensemble (Plan 4 v1 推奨)
- Loop terminator: `all_pass` / `diff_unchanged` / `loops==max_loops` の 3 種
- Memory 3-layer (`summary` / `context` / `content`) 構造を `MemoryEntry` Pydantic で model 化
- `MemoryClient` Protocol → 2 impl (`KaguraCloudClient` + `LocalMemoryClient` SQLite)

---

## 8. Out of Scope (Plan 2 でやらないこと)

- Memory cloud の auth verify (Plan 3 recall smoke に defer)
- 複数 platform 対応の完全 coverage (Linux/Debian + macOS のみ v1、Windows native は hint のみ)
- Plan 4 review loop の fix backend 実装 (Plan 2 では step 2.2 の "credential cache 存在" のみ提供、判定 helper は Plan 4 で import)
- Plan 5 memory client 実装 (Plan 2 は reachability のみ)
- **`Config.workspace_id` 追加は Plan 2 着手時の Task 0 として同時投入** (Q1 spec call で確定、`src/kagura_engineer/config.py` の編集 1 commit + 関連 test 更新)
- Plan 3-5 の他 Open Q (Q3, Q5, Q7-Q9, Q11-Q12) はそれぞれ plan 着手時に再 spec call

---

## 9. 完了条件 (Plan 2 Done)

- [ ] Task 0: `Config.workspace_id` 追加 + 関連 test 緑
- [ ] `setup` (no flag) で doctor の FAIL/WARN が 0 になる(repeatable、sudo 自動 default)
- [ ] `setup --fix <name>` が 7 step どれでも単独実行可能
- [ ] `setup --no-input` が対話環境外 (CI) で clean に走る or loud fail
- [ ] `setup --dry-run` が全 step を preview のみで表示、exit 0/1 で feasibility 判定
- [ ] `setup --json` が `SetupReport` を吐く
- [ ] 全 step に test あり (`monkeypatch` で `shutil.which` / `subprocess.run` / `Path.home` / `urllib.request.urlopen`)
- [ ] 50 → 70+ tests green
- [ ] Plan 3 の `run` から `setup --no-input` を呼んで guard として動く(Plan 3 側の placeholder が埋まる)
- [ ] `#10` review 指摘 (stub exit code) 解決: `cli.py` の `setup` / `run` stub 削除 + exit code contract `0/1/2` に統一
- [ ] README の "Phase 2" セクションが `setup` 実装と一致
- [ ] 共有 helper `resolve_anthropic_auth()` を Plan 4 fix_backend 判定から import 可能 (型 / 関数シグネチャ確定)
