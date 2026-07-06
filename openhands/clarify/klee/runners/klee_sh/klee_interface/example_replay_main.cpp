// 自包含的 KLEE / replay 示例：
//   - solve 模式（KLEE_SOLVE）  ：x 被符号化，跑出 3 条路径 → 3 个 ktest。
//   - replay 模式（KLEE_REPLAY）：用 -lkleeRuntest 链接，KTEST_FILE 注入具体 x 字节，
//                                    在 stdout 上打印 "##REPLAY_JSON## {...}"，
//                                    供 klee_interface/replay_workspace.sh 提取。
//
// 与 example_symbolic_main.cpp 的差别：那个最小例子从不 printf，
// 因此 replay 端拿不到 ##REPLAY_JSON##，固定被记为 crashed。本文件补这一块。
#include <klee/klee.h>
#include <cstdio>

int main() {
  int x = 0;
  klee_make_symbolic(&x, sizeof x, "x");

  int branch = 0;
  if (x > 10) {
    branch = 1;
  } else if (x < -5) {
    branch = 2;
  } else {
    branch = 0;
  }

#if defined(KLEE_REPLAY)
  // 单行：##REPLAY_JSON## <json>
  std::fputs("##REPLAY_JSON## ", stdout);
  std::printf("{\"x\":%d,\"branch\":%d}\n", x, branch);
  std::fflush(stdout);
#endif

  return branch;
}
