// Minimal example for ./klee_stable.sh solve (needs KLEE_CPP_FLAGS unset or use this inside klee image).
#include <klee/klee.h>

int main() {
  int x = 0;
  klee_make_symbolic(&x, sizeof x, "x");
  if (x > 10) {
    return 1;
  }
  if (x < -5) {
    return 2;
  }
  return 0;
}
