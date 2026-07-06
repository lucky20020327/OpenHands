#!/usr/bin/env bash
# Invoked *inside* the KLEE container.
# 输入：一个已经跑过 solve_workspace.sh 的 workspace（含 .cpp + klee-out/）。
# 行为：
#   1) 用 -lkleeRuntest 链接整套 .cpp，编译出原生 replay 二进制；
#   2) 对 klee-out/test*.ktest 逐条 KTEST_FILE=... ./replay_bin 跑一遍，
#      抓取 stdout 里 "##REPLAY_JSON## ..." 这一行，落到 replay/testNNNNNN.json；
#      同时保留完整 stdout/stderr/exit 三个文件用于排错。
#   3) 写一份 replay/index.json 概览。
#
# Usage:
#   replay_workspace.sh [--cpp-std STD] [--cpp-flags FLAGS]
#                       [--parallel N] [--list FILE]
#                       <workspace>
#
# Defaults: --cpp-std -std=c++17, --parallel 1
# --list FILE 内每行一个 testNNNNNN 或 testNNNNNN.ktest（# 开头与空行忽略）；
# 仅这些 ktest 会被重放，其他跳过。
set -euo pipefail

CPP_STD="-std=c++17"
EXTRA_FLAGS=""
PARALLEL="1"
LIST_FILE=""
WS=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --cpp-std)       CPP_STD="$2"; shift 2 ;;
    --cpp-std=*)     CPP_STD="${1#*=}"; shift ;;
    --cpp-flags)     EXTRA_FLAGS="${EXTRA_FLAGS:+$EXTRA_FLAGS }$2"; shift 2 ;;
    --cpp-flags=*)   EXTRA_FLAGS="${EXTRA_FLAGS:+$EXTRA_FLAGS }${1#*=}"; shift ;;
    --parallel)      PARALLEL="$2"; shift 2 ;;
    --parallel=*)    PARALLEL="${1#*=}"; shift ;;
    --list)          LIST_FILE="$2"; shift 2 ;;
    --list=*)        LIST_FILE="${1#*=}"; shift ;;
    -h|--help)
      sed -n '2,19p' "$0"
      exit 0 ;;
    --) shift; WS="${1:-}"; break ;;
    -*) echo "unknown flag: $1" >&2; exit 2 ;;
    *)  WS="$1"; shift ;;
  esac
done

[[ -n "${WS}" ]] || { echo "usage: replay_workspace.sh [--cpp-std STD] [--cpp-flags FLAGS] [--parallel N] [--list FILE] <workspace>" >&2; exit 2; }
[[ "${PARALLEL}" =~ ^[1-9][0-9]*$ ]] || { echo "ERROR: --parallel must be a positive integer (got: ${PARALLEL})" >&2; exit 2; }

# 解析 LIST_FILE 为绝对路径（cd 到 WS 后相对路径会失效）
if [[ -n "${LIST_FILE}" ]]; then
  [[ -f "${LIST_FILE}" ]] || { echo "ERROR: --list file not found: ${LIST_FILE}" >&2; exit 1; }
  case "${LIST_FILE}" in
    /*) : ;;
    *)  LIST_FILE="$(cd "$(dirname "${LIST_FILE}")" && pwd)/$(basename "${LIST_FILE}")" ;;
  esac
fi

cd "$WS"
[[ -d "klee-out" ]] || { echo "ERROR: ${WS}/klee-out not found (run solve first)" >&2; exit 1; }

# --- 定位 KLEE 头/库 -----------------------------------------------------------
KLEE_H="$(find /tmp /usr /home -name klee.h 2>/dev/null | grep '/klee/klee.h$' | head -1)"
[[ -n "${KLEE_H}" ]] || { echo "ERROR: klee.h not found" >&2; exit 1; }
KLEE_INCLUDE="$(dirname "$(dirname "$KLEE_H")")"

# libkleeRuntest 在不同 KLEE 镜像里位置略有差异：
# 1) 先按已知候选目录探一遍；
# 2) 失败再 find /tmp /usr 兜底定位（官方镜像常见在 /tmp/klee_build*stp_z3/lib）。
KLEE_LIB=""
for cand in \
    "$(klee-config --libdir 2>/dev/null || true)" \
    "/tmp/klee_build/lib" \
    "/tmp/klee_src/build/lib" \
    "/usr/lib/x86_64-linux-gnu" \
    "/usr/local/lib"; do
  [[ -z "${cand}" ]] && continue
  if find "${cand}" -maxdepth 2 -name 'libkleeRuntest*' 2>/dev/null | grep -q .; then
    KLEE_LIB="${cand}"; break
  fi
done
if [[ -z "${KLEE_LIB}" ]]; then
  # find 可能因 SIGPIPE 而被 set -e 杀掉；包到子 shell 里 || true 兜住。
  hit="$(set +o pipefail; find /tmp /usr /opt /home -name 'libkleeRuntest*' 2>/dev/null | head -1 || true)"
  [[ -n "${hit}" ]] && KLEE_LIB="$(dirname "${hit}")"
fi
[[ -n "${KLEE_LIB}" ]] || { echo "ERROR: libkleeRuntest not found" >&2; exit 1; }

echo "==> klee include: ${KLEE_INCLUDE}"
echo "==> klee lib    : ${KLEE_LIB}"

# --- 编译 replay 可执行 -------------------------------------------------------
shopt -s nullglob
# Exclude ``dump_layout.cpp``: see solve_workspace.sh — it is a host-side
# layout tool (its own ``int main()``) and would collide with ``harness.cpp``
# during native link.
mapfile -t CPPS < <(find . -name '*.cpp' -not -name 'dump_layout.cpp' | LC_ALL=C sort)
[[ "${#CPPS[@]}" -gt 0 ]] || { echo "ERROR: no .cpp under ${WS}" >&2; exit 1; }

REPLAY_BIN="$(pwd)/replay_bin"
echo "==> compiling replay binary: ${REPLAY_BIN}"
# 用 g++ 做最终链接：部分镜像里 clang++ 调 ld 时 posix_spawn 会被宿主 seccomp 拦下
# （compile-only 不会触发，因此 solve 阶段不受影响）。replay 只是普通原生二进制，
# 用系统 g++ 同样能正确链接 -lkleeRuntest。
CXX_BIN="${CXX:-}"
if [[ -z "${CXX_BIN}" ]]; then
  if command -v g++ >/dev/null 2>&1;       then CXX_BIN="g++"
  elif command -v clang++ >/dev/null 2>&1; then CXX_BIN="clang++"
  else echo "ERROR: no g++/clang++ in container" >&2; exit 1
  fi
fi
echo "==> using compiler: ${CXX_BIN}"
${CXX_BIN} ${CPP_STD} -O0 -g -Wall -Wextra \
    -DKLEE_SOLVE=1 -DKLEE_REPLAY=1 \
    -I. "-I${KLEE_INCLUDE}" ${EXTRA_FLAGS} \
    "${CPPS[@]}" \
    "-L${KLEE_LIB}" "-Wl,-rpath,${KLEE_LIB}" -lkleeRuntest \
    -o "${REPLAY_BIN}"

# --- 准备输出目录 -------------------------------------------------------------
rm -rf replay
mkdir -p replay

mapfile -t KTESTS_ALL < <(find klee-out -maxdepth 1 -name 'test*.ktest' | LC_ALL=C sort)

# --- list 过滤（如指定）------------------------------------------------------
KTESTS=()
if [[ -n "${LIST_FILE}" ]]; then
  declare -A WANTED=()
  while IFS= read -r line || [[ -n "${line}" ]]; do
    # 去掉行尾 \r、首尾空白；忽略空行与 # 注释
    line="${line%$'\r'}"
    line="${line#"${line%%[![:space:]]*}"}"
    line="${line%"${line##*[![:space:]]}"}"
    [[ -z "${line}" || "${line:0:1}" == "#" ]] && continue
    base="$(basename "${line}")"
    base="${base%.ktest}"
    WANTED["${base}"]=1
  done < "${LIST_FILE}"
  if [[ "${#WANTED[@]}" -eq 0 ]]; then
    echo "ERROR: --list ${LIST_FILE} 内没有有效条目" >&2; exit 1
  fi
  for kt in "${KTESTS_ALL[@]}"; do
    base="$(basename "${kt}" .ktest)"
    [[ -n "${WANTED[${base}]:-}" ]] && KTESTS+=("${kt}")
  done
  echo "==> filtered by --list: ${#KTESTS[@]} / ${#KTESTS_ALL[@]} ktests"
  # 报告 list 中未命中的条目，便于排错
  for want in "${!WANTED[@]}"; do
    if [[ ! -f "klee-out/${want}.ktest" ]]; then
      echo "    [list] missing in klee-out: ${want}" >&2
    fi
  done
else
  KTESTS=("${KTESTS_ALL[@]}")
fi

TOTAL="${#KTESTS[@]}"
echo "==> replay over ${TOTAL} ktests (parallel=${PARALLEL})"

if [[ "${TOTAL}" -eq 0 ]]; then
  cat > replay/index.json <<EOF
{"total":0,"ok":0,"failed":0,"items":[]}
EOF
  echo "==> done (no ktests)"
  exit 0
fi

# --- 单条 replay 的 worker（在子进程里跑），结果落到 replay/<base>.idx ----------
# .idx 文件一行 JSON：{"ktest":"...","status":"ok|crashed","exit":N}
# 主进程稍后扫描所有 .idx 聚合 index.json，避免并发写共享变量。
run_one_ktest() {
  local kt="$1"
  local base; base="$(basename "${kt}" .ktest)"
  local stdout_file="replay/${base}.stdout"
  local stderr_file="replay/${base}.stderr"
  local exit_file="replay/${base}.exit"
  local json_file="replay/${base}.json"
  local idx_file="replay/${base}.idx"

  local rc=0
  KTEST_FILE="${kt}" "${REPLAY_BIN}" >"${stdout_file}" 2>"${stderr_file}" || rc=$?
  echo "${rc}" > "${exit_file}"

  # grep 没匹配会返回 1；包到子 shell 里 || true 兜住，避免 set -e 杀掉 worker。
  local json_line
  json_line="$( { grep -F '##REPLAY_JSON## ' "${stdout_file}" | tail -1 | sed 's/^##REPLAY_JSON## //'; } 2>/dev/null || true )"

  local status
  if [[ -n "${json_line}" ]]; then
    printf '%s\n' "${json_line}" > "${json_file}"
    status="ok"
  else
    printf '{"ktest":"%s","replay_status":"crashed","exit_code":%s}\n' "${base}" "${rc}" > "${json_file}"
    status="crashed"
  fi

  printf '{"ktest":"%s","status":"%s","exit":%s}\n' "${base}" "${status}" "${rc}" > "${idx_file}"
}

# --- 调度：N 路并发，wait -n 信号量 ------------------------------------------
running=0
for kt in "${KTESTS[@]}"; do
  if [[ "${PARALLEL}" -le 1 ]]; then
    run_one_ktest "${kt}"
  else
    if [[ "${running}" -ge "${PARALLEL}" ]]; then
      # 等任意一个 worker 退出再继续投入下一个；wait -n 不存在于 bash<4.3，
      # 但 KLEE 镜像内的 bash 都是 5.x，足够用。
      # `|| true`：worker 已自行落 .idx/.exit，即使它因系统层异常非 0 退出，
      # 也不能让 set -e 把整个调度器一起带走、丢掉剩下的 ktest。
      wait -n || true
      running=$((running - 1))
    fi
    run_one_ktest "${kt}" &
    running=$((running + 1))
  fi
done

# 等所有后台 worker 收尾（同样吞掉非 0，理由同上）
if [[ "${PARALLEL}" -gt 1 ]]; then
  wait || true
fi

# --- 聚合 index.json（按 ktest 排序，输出稳定）-------------------------------
OK=0
FAIL=0
INDEX_ITEMS=""
mapfile -t IDX_FILES < <(find replay -maxdepth 1 -name '*.idx' | LC_ALL=C sort)
for idx in "${IDX_FILES[@]}"; do
  line="$(cat "${idx}")"
  case "${line}" in
    *'"status":"ok"'*)      OK=$((OK + 1)) ;;
    *'"status":"crashed"'*) FAIL=$((FAIL + 1)) ;;
  esac
  if [[ -n "${INDEX_ITEMS}" ]]; then INDEX_ITEMS="${INDEX_ITEMS},"; fi
  INDEX_ITEMS="${INDEX_ITEMS}${line}"
done

cat > replay/index.json <<EOF
{"total":${TOTAL},"ok":${OK},"failed":${FAIL},"items":[${INDEX_ITEMS}]}
EOF

echo "==> done: ok=${OK} failed=${FAIL} -> ${WS}/replay"
