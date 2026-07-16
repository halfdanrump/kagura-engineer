# Plan 3 — `run` コマンド (memory-grounded agent loop / idea→PR)

> [!NOTE]
> Historical design record. It may describe pre-implementation state, internal
> plan numbers, or behavior that has since changed. See the
> [current README](../../README.md) and [document map](../README.md).

**Status:** 設計 (implementation not started)
**Date:** 2026-06-07
**Author:** kagura-engineer dev
**Depends on:** Plan 1 (doctor) — done · Plan 2 (setup) — done at v0.1.0 (186 tests green, main は branch protection 済)

---

## 0. 前提と位置づけ

### 0.1 これは launch gate

`kagura-engineer` の public 公開は `run` が end-to-end で動くまで中断、と確定済み(decision `b774ffe7`)。public 準備(Apache-2.0 / CI / community health)は完了・凍結=「装填済み」。**`run` が実際に issue→PR を回す瞬間が launch trigger。** Plan 3 はその本丸。

### 0.2 指導原則(この設計の背骨)

1. **bounded composable + 単一 orchestrator**
   3 層 coding harness(decision `c2a7e94e`): actor=`kagura-engineer`(Apache-2.0) / workflow=`gh-issue-driven`(MIT, 明示依存) / persistence=`memory-cloud`。
   **`run` は他 agent(reviewer / planner)を束ねない。** workflow=gh-issue-driven が唯一の orchestration 層であり、`run` はそれを起動する actor 側ループ。reviewer(別プロダクト `kagura-code-reviewer`)は PR 後に *別起動* で引き継ぐ — `run` は呼ばない。

2. **`run` = memory-grounded agent loop**
   `run` は「ただの launcher」ではなく、**recall→act→persist** のエージェントループ。Claude Code(headless `claude -p`)が「手」、memory-cloud が「記憶」、gh-issue-driven が「手順書」。

3. **HITL はダイヤルであって fork ではない**
   「自律」と「人間ゲート」は別軸。v1 は gate ON(`trust before integration`)。記憶が育つほど(失敗→fix の `prevents` edge が蓄積するほど)人間ゲートを下げられる。`--unattended` 方向への移行は後続 plan。**memory がオートノミーを稼ぐ。**

### 0.3 確定済みの足場(Plan 1 / Plan 2 から継承)

- `setup --no-input` が `SetupReport` を返し、`is_blocked` が真なら `run` は起動しない(Plan 2 `setup/__init__.py` docstring に明記済の contract)。
- doctor は per-check 例外 isolation(`registry.run_all`)。setup も per-step isolation(`run_plan`)。**同じ try/except 方針を `run` の per-phase にも適用。**
- `Config` は `profile / memory_cloud_url / workspace_id / context_id / ollama_url / review` を持つ(`config.py`)。Plan 3 はこれをそのまま使う(`context_id` の用途をここで確定 — §4.1)。
- 外部コマンドは全て `subprocess.run`(`setup/gh.py`・`claude.py`・`ollama.py`・`install.py`)。`run` の `claude -p` も同流儀、新規依存ゼロ。
- `install.py` の `stderr_tail` ヘルパを `run` の失敗 report でも再利用。

---

## 1. v1 スコープ

### 1.1 In scope

- **入力 = `kagura-engineer run <issue#>`、出力 = GitHub PR 1 本**(gate2 内部 advisor review 込み、HITL gate ON)。
- 最小の memory client(`recall` / `remember` / `get_state` / `set_state` / `load_pinned`)を Plan 3 に前倒し(= Plan 5 の一部を引く)。記憶接地が agent の核なので前倒しの価値あり。
- per-run worktree 隔離 + resume(context window が死んでも再開可能)。
- doctor に gh-issue-driven 存在 check を `is_blocking` で追加(§4.2)。

### 1.2 Out of scope(後続 plan へ)

| 項目 | 行き先 |
|---|---|
| standalone reviewer(`kagura-code-reviewer`)連結(review + gate) | Plan 4 ✅(v1 done) |
| auto-review/auto-fix loop(red → claude -p fix → re-review) | Plan 4b ✅(`review --fix`) |
| `/gh-issue-driven:goal`(milestone 丸ごと多 issue 自律) | v0.3 / 後続 |
| `--unattended`(HITL ダイヤルを OFF 方向へ) | 後続 |
| `LocalMemoryClient`(SQLite offline) | Plan 5 |
| `explore`(Hebbian graph)のリッチ活用・`feedback` 自動チューニング・Sleep 連携 | Plan 5+ |
| `KaguraAgent`(SDK の高位ループ抽象)への置換 | 将来レバー |
| free-form task 文字列入力(issue を持たない idea→PR) | 後続(`/propose` 経由) |

---

## 2. 設計の全体像

### 2.1 CLI surface

```python
@app.command()
def run(
    issue: int = typer.Argument(..., help="GitHub issue number to drive to a PR"),
    config: str = _CONFIG_OPT,
    no_remember: bool = typer.Option(False, "--no-remember", help="skip memory persist (recall は行う)"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
```

- 既存 stub(`cli.py:119-123`、`echo + exit 2`)を削除して置換。
- `--unattended` は **v1 では出さない**(将来追加する seam だけ残す)。

### 2.2 Module layout

```
src/kagura_engineer/run/
    __init__.py      # orchestrator: run_idea(cfg, issue, ...) → RunReport。agent loop 本体
    memory.py        # MemoryClient Protocol + KaguraCloudClient(kagura-memory SDK wrap)
    worktree.py      # ensure_worktree() / remove_worktree()(command git で RTK 回避)
    workflow.py      # invoke_phase(): claude -p で /gh-issue-driven:<phase> 起動 + verdict 回収
    gate.py          # verdict 解釈 + HITL ダイヤル(red→halt)
    result.py        # RunReport, PhaseResult, RunStatus(pydantic, setup/result.py に倣う)
    render.py        # print_report() + to_json()
```

`__init__.py` のみ CLI が import する(setup と同じ「orchestrator が公開境界」方針)。`memory`/`worktree`/`workflow`/`gate` は private 実装詳細。

### 2.3 Data model

```python
class RunStatus(str, Enum):
    OK = "ok"               # PR 作成まで到達
    BLOCKED = "blocked"     # guard / blocking check / gate halt で停止(resume 可)
    FAIL = "fail"           # hard error

class PhaseResult(BaseModel):
    name: str               # "guard" | "recall" | "start" | "ship" | "persist"
    status: RunStatus
    detail: str
    verdict: str | None = None      # gh-issue-driven の gate verdict(green/yellow/red)
    duration_s: float = 0.0

class RunReport(BaseModel):
    issue: int
    phases: list[PhaseResult]
    pr_url: str | None = None
    worktree: str | None = None
    resume_hint: str | None = None  # BLOCKED 時に「どう再開するか」
    duration_s: float
```

### 2.4 動作モード

| Mode | Behavior |
|---|---|
| `run <issue#>` | full agent loop: guard → recall → worktree → start(gate)→ ship → persist → PR |
| `run <issue#> --no-remember` | recall は行うが persist を skip(dry な試行 / privacy) |
| `run <issue#> --json` | `RunReport` を JSON で stdout |

### 2.5 Exit code(setup と統一)

| code | 意味 |
|---|---|
| 0 | PR 作成成功(`RunStatus.OK`) |
| 1 | hard fail(`RunStatus.FAIL`) |
| 2 | blocked: guard fail / blocking doctor check / gate halt(human 必要、resume 可) |
| 2 | config / unknown issue エラーも 2 |

---

## 3. Agent loop(1 run の中身)

```
run <issue#>
  0. guard
       setup(cfg, no_input=True) → SetupReport.is_blocked なら exit 2
       doctor blocking check(gh-issue-driven 存在 §4.2)→ FAIL なら exit 2
  1. recall
       load_pinned(context_id)              … repo の guardrail / goal を deterministically
       recall(context_id, query=issue 文脈, trust_tier="trusted")  … 関連 decision/pattern/prevents
       get_state(context_id, key="run:<issue#>")  … 中断していたら resume point
  2. worktree
       ensure_worktree("run-<issue#>")      … 無ければ作る / あれば resume
  3. act: start
       claude -p "/gh-issue-driven:start <issue#>"(worktree 内、recall 結果を grounding に注入)
       verdict 回収 → gate.evaluate()
         red    → set_state(resume) + 人間に surface + RunStatus.BLOCKED, exit 2
         green/yellow → 継続
  4. act: ship
       claude -p "/gh-issue-driven:ship"
       verdict 回収 → gate.evaluate()(同上)
       PR url を回収
  5. persist(--no-remember で skip)
       remember(savepoint: issue→PR の結果)
       失敗 phase があれば remember(failure, linked prevents → fix)
       set_state(key="run:<issue#>", value=done marker)
  → RunStatus.OK, PR url 出力, exit 0
```

### 3.1 なぜ phase 毎に `claude -p` を分けるか

gh-issue-driven は **resumable な branch + memory checkpoint** を前提に設計されている(`start` が typed branch を作り checkpoint、`ship` がそれを読む)。よって phase を別 `claude -p` 起動に分割しても、各起動が branch/memory から state を読み直して継続できる。**HITL ダイヤルは phase 間の `run` 側 Python ループに自然に収まる**(setup の `NEEDS_USER` 検出と同型)。headless(非対話)と HITL の矛盾はこの「phase 間で止める」で解消する。

### 3.2 verdict の回収方法

`gate.evaluate()` には gh-issue-driven の gate verdict(green/yellow/red)が要る。v1 の回収は **2 段 fallback**:
1. `claude -p` の phase 出力末尾に出る既知マーカー(gh-issue-driven が出す verdict 行)を `workflow.invoke_phase` がパースして返す。
2. パース不能なら follow-up `claude -p "/gh-issue-driven:status"`(read-only)で phase/verdict を確定。

パースが脆い場合に備え、**verdict 不明は安全側に倒して red 扱い**(= HITL halt)。誤って green と誤認して暴走するより、止めて人間に見せる方が `trust before integration` に合う。

### 3.3 grounding の注入方法

`recall` / `load_pinned` の結果を `claude -p` の prompt 前段に **コンテキストブロックとして文字列注入**する(v1)。注入 budget は **pinned ~2k + on-demand recall ~4k tokens**(config 上限、Plan 2 Open Q12 を確定)。headless Claude Code 自身に memory MCP を持たせて in-task recall させるのは **out of scope**(bounded に保つ、Plan 5+)。

---

## 4. 宙に浮いていた決定の解決

### 4.1 `context_id` の用途(review leftover #8)

**確定: `cfg.context_id` = `run` が接地する Memory Cloud の project context。** durable な recall/remember はこの context に対して行う。
- per-run の working/resume state は **別 context を作らず** `get_state`/`set_state`(key=`run:<issue#>`)で持つ。context を run 毎に作ると context 数が爆発するため。
- これで Plan 2 Open Q5(context_id 寿命 = per-project + per-run sub-context)を **「per-project context + per-run state key」** という形で着地。"sub-context" は別 context ではなく state namespace として実現。

### 4.2 `CheckResult.is_blocking` の用途(review leftover #9)

**確定: `is_blocking` = 「`run` 起動前に必ず通すべき check」のマーカー。**
- doctor に新 check `gh-issue-driven`(プラグイン + `gh` + `claude` の存在/起動可能性)を追加し `is_blocking=True`。
- `run` の guard(§3 phase 0)は doctor の blocking check が全 OK でなければ即 exit 2。blocking でない WARN(例: memory-cloud 4xx)は run を止めない。
- これにより「`run` が headless セッションの奥深くで意味不明に死ぬ」前に、前段で明確に弾ける。

> 注: review leftover `38508d20` の教訓 — severity は実コードで検証する。`is_blocking` 追加時に既存 check の severity 表を見直し、過大評価を持ち込まない。

---

## 5. memory client(SDK wrap)

### 5.1 依存

- `kagura-memory` SDK(配布名 `kagura-memory` `0.29.0`、import `kagura_memory`)を `pyproject.toml` の dependencies に追加。
- SDK の `KaguraClient` が `recall / remember / get_state / set_state / load_pinned / explore / feedback / recall_upcoming / reference` を提供(実体検証済)。

### 5.2 Protocol(Plan 2 §7 構想の最小着地)

```python
class MemoryClient(Protocol):
    def load_pinned(self, context_id: str) -> list[Memory]: ...
    def recall(self, context_id: str, query: str, *, k: int, trust_tier: str | None) -> list[Memory]: ...
    def remember(self, context_id: str, *, summary: str, content: str, type: str, **kw) -> str: ...
    def get_state(self, context_id: str, key: str) -> dict | None: ...
    def set_state(self, context_id: str, key: str, value: dict) -> None: ...
```

- v1 実装は `KaguraCloudClient`(SDK wrap)1 つ。
- `LocalMemoryClient`(SQLite offline)は Plan 5。Protocol を切ることでテストは fake 実装で完結(実 Memory Cloud に依存しない)。

### 5.3 auth / 失敗時

- SDK の認証は workspace-scoped API key(`Config.workspace_id` と整合)。env / keyring 解決順は Plan 2 の `resolve_token` helper を流用。
- `KaguraAuthError` / `KaguraConnectionError` は **blocking**。記憶接地が agent の核なので degrade して走らせない(guard で止める)。

---

## 6. worktree

- 命名: `run-<issue#>`(衝突時 `run-<issue#>-<n>`)。resume が同じ worktree を見つけられる決定性を優先(Plan 2 Open Q11 の `run-<short-id>` を issue 駆動に合わせて確定)。
- 配置: repo working tree を汚さないため **repo 外の専用 dir**(default `<repo>/../.kagura-runs/<repo>/run-<issue#>`、config で上書き可)。
- **削除は `command git worktree remove`**(RTK proxy が `git worktree remove` を usage error で落とす既知の dev-env gotcha、troubleshooting `f01e3167`)。worktree 系 git 操作は RTK を通さない。
- gh-issue-driven の `start` が typed branch を作るので、`run` は worktree の用意(base=main の checkout)までを担当し、branch 作成は workflow に委ねる。

---

## 7. エラー処理

- 各 phase の `claude -p` は timeout 付き subprocess。非 0 / timeout → `PhaseResult(status=FAIL)`、`stderr_tail` で末尾を report。
- per-phase 例外 isolation: doctor/setup と同じ try/except で、1 phase の例外が loop 全体を巻き込まない(leak は FAIL phase に変換)。
- gate red / blocking guard → `RunStatus.BLOCKED` + `resume_hint`(「`run <issue#>` を再実行すれば worktree と state から再開」)。
- `--json` 時も同じ `RunReport` を吐く(exit code は別途)。

---

## 8. テスト戦略

CI で実 Claude / 実 Memory Cloud は叩けない。よって境界を mock する。

- `claude -p`: `subprocess.run` を monkeypatch。verdict 文字列(green/yellow/red)・非 0・timeout を注入し、gate 分岐と exit code を検証。
- `MemoryClient`: Protocol の fake 実装(in-memory dict)で recall/remember/get_state/set_state を差し替え、loop の recall→persist と resume(set_state→get_state)を検証。
- `gate.evaluate()`: verdict heuristic 単体 test(green/yellow→継続、red→halt)。
- `worktree`: `command git` 呼び出しを mock(実 worktree を作らない)、命名/衝突/resume を検証。
- guard: `setup` の `SetupReport.is_blocked` と doctor blocking check の組合せ(blocked→exit 2)。
- `--json` 出力、`--no-remember`(persist skip)、exit code matrix。
- 目標: 186 → 210+ tests green。

---

## 9. 実装順序

1. **Task 0**: `pyproject.toml` に `kagura-memory>=0.29` 追加。`run/memory.py`(Protocol + `KaguraCloudClient` wrap)+ fake 実装 + test。
2. `run/result.py`(`RunReport`/`PhaseResult`/`RunStatus`)+ test(setup/result.py に倣う)。
3. `run/worktree.py`(`command git` ベース)+ test。
4. `run/workflow.py`(`invoke_phase` = `claude -p` + 出力回収)+ test。
5. `run/gate.py`(verdict heuristic + HITL halt)+ test。
6. doctor に `gh-issue-driven` blocking check 追加(`is_blocking` 導入)+ test(#9 解決)。
7. `run/__init__.py` orchestrator(agent loop)+ `render.py` + test。
8. `cli.py` の `run` stub 削除 + 新 command 配線(exit code contract)。
9. README の Plan 3 行を「stub」から実装済へ更新。
10. E2E smoke(mock 境界で recall→start→ship→persist→PR の full path)。

---

## 10. 完了条件 (Plan 3 Done)

- [ ] `run <issue#>` が guard→recall→worktree→start(gate)→ship→persist→PR を通す
- [ ] recall(`load_pinned`+`recall`)結果が `claude -p` の grounding に注入される
- [ ] gate red で `BLOCKED`(exit 2)+ `set_state` resume、再実行で worktree/state から再開
- [ ] doctor の `gh-issue-driven` blocking check が run guard を弾ける(#9 解決)
- [ ] `cfg.context_id` を project context として recall/remember、per-run は `run:<issue#>` state(#8 解決)
- [ ] `MemoryClient` Protocol + `KaguraCloudClient`、test は fake で完結
- [ ] `--no-remember` / `--json` / exit code matrix の test
- [ ] worktree 削除が `command git` 経由(RTK 回避)
- [ ] 186 → 210+ tests green
- [ ] README の Plan 3 が実装と一致(stub 記述削除)
- [ ] `cli.py` の run stub 削除 + exit code `0/1/2` 統一

---

## 11. Out of scope の再確認(やらないこと)

- reviewer(`kagura-code-reviewer`)連結・auto-review/fix loop(Plan 4)
- `/goal` 多 issue 自律・`--unattended`(後続)
- `LocalMemoryClient` SQLite・`explore`/`feedback`/Sleep のリッチ活用(Plan 5+)
- headless Claude Code への memory MCP 直付け(in-task recall)— v1 は run 側で文字列注入のみ
- free-form task 文字列入力(issue なし idea→PR)— 後続で `/propose` 経由
- `KaguraAgent`(SDK 高位ループ)への置換 — 将来レバー

### 11.1 実装時に確定した v1 deferral(設計 §3 からの絞り込み)

実装(2026-06-07)で以下を v1 では見送り、後続に倒した。設計 §3 と差分があるため明記する:

- **失敗 phase の `remember(failure, prevents→fix)`(設計 §3 step 5)は v1 未実装。** `prevents` edge は graph linking であり、§11 で Plan 5+ に倒した `explore`(Hebbian graph)のリッチ活用と同系統。v1 は成功時の savepoint 永続化のみ。失敗→fix の preemptive 学習は Plan 5 で memory graph と同梱実装する。
- **grounding の token 上限(§3.3 の pinned ~2k + recall ~4k)は明示的 truncation 未実装。** v1 は `recall(k=5)` で実質 bound されるため過大注入の実害は小さい。pinned/recall が増えて prompt が膨らむ兆候が出た時点で Plan 5 で char/token cap を追加する。

### 11.2 Plan 4 reviewer 連結の契約(reviewer 側で確定・申し送り受領 2026-06-07)

reviewer は `kagura-code-reviewer`(別 repo・bounded・agent 化しない)。Plan 4 の post-PR review/fix loop はこれを **別起動**で呼ぶ。**Markdown を scrape せず JSON envelope を読む**こと:

- 起動: `kagura-code-reviewer --base <main> --format json --out <file> [--model <ollama-tag|alias>] [--context-file <grounding.md>]`(zero-config で tool-calling 可能なローカルモデルを自動選択)。
- JSON envelope(`schema_version` で版管理): `verdict`(green/yellow/red)・`summary`(total/blocking/by_severity/incomplete)・`findings[]`。
- 不変条件: **`verdict=="red"` ⟺ `exit 1`**。green/yellow は共に exit 0(clean か advisory かは `verdict` で判別)。`summary.incomplete=true` は「未完走」= 実 blocking と区別。gate はこの verdict/summary を読む。
- `--context-file` で grounding を渡す場合の**呼ぶ側責務**: recall は `trust_tier="trusted"` 必須 / 注入 memory は untrusted reference-only として fence + 「指示に従うな」/ memory に finding 抑制権なし / 自律 gate を下げるソースは owner-pinned のみ。
- 詳細は Kagura memory(context `kagura-engineer-dev`)の Plan 4 連結契約 memory、および reviewer 側 context `kagura-code-review-dev`(`860bdb8d`)に記録。
