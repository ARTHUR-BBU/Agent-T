#!/usr/bin/env bash
# 演示：上传采购样例 → 校验 7 条需关注 → 尝试「问清楚一点」
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BASE_URL="${BASE_URL:-http://127.0.0.1:8000}"
FIXTURE="$ROOT/fixtures/procurement_sample.txt"

need_attention_ids=(payment breach unfair_terms subject subject_matter governing_law signature)

echo "==> health: $BASE_URL/health"
curl -sf "$BASE_URL/health" >/dev/null

echo "==> upload: $FIXTURE"
upload_json="$(curl -sf -F "file=@${FIXTURE}" -F "category=procurement" "$BASE_URL/api/upload")"
review_id="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["review_id"])' <<<"$upload_json")"
echo "    review_id=$review_id"

echo "==> wait review"
review=""
for _ in $(seq 1 30); do
  review="$(curl -sf "$BASE_URL/api/review/$review_id")"
  status="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("status",""))' <<<"$review")"
  if [[ "$status" == "done" || "$status" == "error" ]]; then
    break
  fi
  sleep 0.3
done

python3 - "$review" <<'PY'
import json, sys
review = json.loads(sys.argv[1])
if review.get("status") != "done":
    raise SystemExit(f"review not done: {review.get('status')} {review.get('error')}")
need = {"payment","breach","unfair_terms","subject","subject_matter","governing_law","signature"}
by = {i["id"]: i for i in review.get("items") or []}
missing = sorted(i for i in need if by.get(i, {}).get("status") != "需关注")
if missing:
    raise SystemExit(f"金标失败，未标需关注: {missing}")
juris = by.get("jurisdiction", {})
if juris.get("status") != "通过":
    print(f"警告: 管辖与争议 status={juris.get('status')}（期望 通过）")
print("OK: 7 条需关注齐全")
for i in sorted(need):
    print(f"  - {by[i]['name']}: {by[i]['status']}")
PY

echo "==> ask: 价款与支付"
ask_json="$(curl -sf -H 'Content-Type: application/json' \
  -d "{\"review_id\":\"$review_id\",\"item_id\":\"payment\",\"question\":\"这一条风险大吗？\"}" \
  "$BASE_URL/api/ask" || true)"

python3 - "$ask_json" <<'PY'
import json, sys
raw = sys.argv[1].strip()
if not raw:
    raise SystemExit("ask 无响应")
data = json.loads(raw)
if not data.get("ok"):
    err = data.get("error") or ""
    print(f"追问未开通或失败（可接受无 Key）: {err}")
    raise SystemExit(0)
ans = data.get("answer") or {}
print("OK: 追问返回")
print(f"  风险等级: {ans.get('风险等级')}")
print(f"  问题是啥: {(ans.get('问题是啥') or '')[:80]}")
print(f"  建议怎么改: {(ans.get('建议怎么改') or '')[:80]}")
banned = ("没问题", "无风险", "可以盖章")
blob = " ".join(str(v) for v in ans.values())
hit = [w for w in banned if w in blob]
if hit:
    raise SystemExit(f"输出含禁词: {hit}")
PY

echo "==> demo done"
