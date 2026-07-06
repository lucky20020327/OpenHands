#!/usr/bin/env bash
# Invoked *inside* the KLEE container. Workspace must contain one or more .cpp files
# (and any local .h/.hpp). Produces <workspace>/klee-out/.
#
# Usage:
#   solve_workspace.sh [--max-seconds N] [--max-forks N] [--only-output-states-covering-new] [--cpp-std STD] [--cpp-flags FLAGS] <workspace>
#
# Defaults: --max-seconds 120 --max-forks 50000 --cpp-std -std=c++17
set -euo pipefail

MAX_SECONDS="120"
MAX_FORKS="50000"
ONLY_OUTPUT_STATES_COVERING_NEW=""
CPP_STD="-std=c++17"
EXTRA_FLAGS=""
WS=""
# Search/memory tuning (empty = use KLEE built-in defaults).
SEARCH=()
USE_BATCHING_SEARCH=""
BATCH_INSTRUCTIONS=""
MAX_MEMORY=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --max-seconds)   MAX_SECONDS="$2"; shift 2 ;;
    --max-seconds=*) MAX_SECONDS="${1#*=}"; shift ;;
    --max-forks)     MAX_FORKS="$2"; shift 2 ;;
    --max-forks=*)   MAX_FORKS="${1#*=}"; shift ;;
    --only-output-states-covering-new) ONLY_OUTPUT_STATES_COVERING_NEW=1; shift ;;
    --search)        SEARCH+=("$2"); shift 2 ;;
    --search=*)      SEARCH+=("${1#*=}"); shift ;;
    --use-batching-search) USE_BATCHING_SEARCH=1; shift ;;
    --batch-instructions)   BATCH_INSTRUCTIONS="$2"; shift 2 ;;
    --batch-instructions=*) BATCH_INSTRUCTIONS="${1#*=}"; shift ;;
    --max-memory)    MAX_MEMORY="$2"; shift 2 ;;
    --max-memory=*)  MAX_MEMORY="${1#*=}"; shift ;;
    --cpp-std)       CPP_STD="$2"; shift 2 ;;
    --cpp-std=*)     CPP_STD="${1#*=}"; shift ;;
    --cpp-flags)     EXTRA_FLAGS="${EXTRA_FLAGS:+$EXTRA_FLAGS }$2"; shift 2 ;;
    --cpp-flags=*)   EXTRA_FLAGS="${EXTRA_FLAGS:+$EXTRA_FLAGS }${1#*=}"; shift ;;
    -h|--help)
      sed -n '2,9p' "$0"
      exit 0 ;;
    --) shift; WS="${1:-}"; break ;;
    -*) echo "unknown flag: $1" >&2; exit 2 ;;
    *)  WS="$1"; shift ;;
  esac
done

if [[ -z "${WS}" ]]; then
  echo "usage: solve_workspace.sh [--max-seconds N] [--max-forks N] [--only-output-states-covering-new] [--cpp-std STD] [--cpp-flags FLAGS] <workspace>" >&2
  exit 2
fi

cd "$WS"

KLEE_H="$(find /tmp /usr /home -name klee.h 2>/dev/null | grep '/klee/klee.h$' | head -1)"
if [[ -z "${KLEE_H}" ]]; then
  echo "ERROR: klee.h not found" >&2
  exit 1
fi
KLEE_INCLUDE="$(dirname "$(dirname "$KLEE_H")")"

shopt -s nullglob
# Exclude ``dump_layout.cpp``: it is a single-shot host tool the builder
# renders into ``<workspace>/klee/`` for ``klee.sh dump-layout``. It defines
# its own ``int main()`` and would collide with ``harness.cpp`` at
# ``llvm-link`` time (``Linking globals named 'main': symbol multiply
# defined!``). It is irrelevant to KLEE solving.
mapfile -t CPPS < <(find . -name '*.cpp' -not -name 'dump_layout.cpp' | LC_ALL=C sort)
if [[ "${#CPPS[@]}" -eq 0 ]]; then
  echo "ERROR: no .cpp under ${WS}" >&2
  exit 1
fi

rm -rf bc klee-out merged.bc
mkdir -p bc

for src in "${CPPS[@]}"; do
  rel="${src#./}"
  # Avoid colliding names from subdirs
  safe="${rel//\//__}"
  safe="${safe%.cpp}"
  outbc="bc/${safe}.bc"
  echo "==> emit-llvm: ${rel} -> ${outbc}"
  # KLEE_SOLVE=1：在 helpers.hpp / harness 里启用 klee_make_symbolic 分支。
  # 没有这个宏，klee_make_symbolic 调用会被预处理器整段去掉 → KLEE 跑出 0 path coverage。
  clang++ ${CPP_STD} -O0 -g -Wall -Wextra -DKLEE_SOLVE=1 \
      -I. "-I${KLEE_INCLUDE}" ${EXTRA_FLAGS} -emit-llvm -c "$rel" -o "$outbc"
done

mapfile -t BCS < <(find bc -name '*.bc' | LC_ALL=C sort)
if [[ "${#BCS[@]}" -eq 0 ]]; then
  echo "ERROR: no bitcode emitted" >&2
  exit 1
fi

echo "==> llvm-link (${#BCS[@]} modules) -> merged.bc"
llvm-link "${BCS[@]}" -o merged.bc

KLEE_ARGS=(
  "--output-dir=${WS}/klee-out"
  "--max-time=${MAX_SECONDS}"
  "--max-forks=${MAX_FORKS}"
  "--libc=uclibc"
)
if [[ -n "${ONLY_OUTPUT_STATES_COVERING_NEW}" ]]; then
  KLEE_ARGS+=("--only-output-states-covering-new")
fi
if [[ ${#SEARCH[@]} -gt 0 ]]; then
  for _s in "${SEARCH[@]}"; do
    KLEE_ARGS+=("--search=${_s}")
  done
fi
if [[ -n "${USE_BATCHING_SEARCH}" ]]; then
  KLEE_ARGS+=("--use-batching-search")
fi
if [[ -n "${BATCH_INSTRUCTIONS}" ]]; then
  KLEE_ARGS+=("--batch-instructions=${BATCH_INSTRUCTIONS}")
fi
if [[ -n "${MAX_MEMORY}" ]]; then
  KLEE_ARGS+=("--max-memory=${MAX_MEMORY}")
fi

echo "==> klee (max-time=${MAX_SECONDS}, max-forks=${MAX_FORKS}${ONLY_OUTPUT_STATES_COVERING_NEW:+, only-output-states-covering-new=true}${MAX_MEMORY:+, max-memory=${MAX_MEMORY}})"
echo "==> klee full args: ${KLEE_ARGS[*]}"
klee "${KLEE_ARGS[@]}" merged.bc

test -d "${WS}/klee-out"
echo "==> done: ${WS}/klee-out"
