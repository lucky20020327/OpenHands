# KLEE Implementation Rules

These rules are rendered into each task workspace as `klee/KLEE_IMPLEMENTATION_RULES.md`.
They are the local coding contract for `mock_<i>.cpp` implementations in the Clarify
KLEE harness.

## Read This First

You are translating the feature request into a KLEE-friendly candidate
implementation. Do not use path pruning to remove real business behavior. First
implement the behavior faithfully, then add the smallest constraints needed to
keep KLEE solve tractable.

## Hard Rules

- Edit only the `mock_<i>.cpp` files in your variant directory — one per entry
  point (paired with `harness_<i>.cpp`). Implement each entry point in its own
  `mock_<i>.cpp`.
- Entry points are solved INDEPENDENTLY: each `mock_<i>.cpp` is linked only with
  its paired `harness_<i>.cpp`, never with another entry point's
  `mock_<j>.cpp`. If one entry point depends on another, model that dependency
  through `call_stub`; do NOT call another entry point's function directly (it
  would not link).
- Keep `#include "klee_helpers.hpp"`, `#include "core_abi.hpp"`, and
  `extern StubTrace stub_calls;`. `klee_helpers.hpp` must be included first
  because it defines `StubTrace`, `call_stub`, `error_code_hash`, and `kstr_*`.
- Do not include original repository headers in `mock_<i>.cpp`; use the POD types
  from `core_abi.hpp` plus local POD request/response structs for stubs.
- Do not use heap allocation, `std::string`, `std::vector`, exceptions, RTTI,
  threads, recursion, or unbounded loops.
- Do not call standard string functions such as `strlen`, `strcmp`, `strcpy`,
  `strcat`, `sprintf`, or `snprintf` on symbolic data.
- Use `error_code_hash("ERROR_NAME")` for non-zero error codes instead of
  hard-coded numeric literals.
- Never use `klee_assume(false)` to swallow a difficult path.

## `klee_assume` Must Not Short-Circuit

`klee_assume` is not a C/C++ control-flow construct. Its argument must be a
straight-line boolean expression whenever possible. Short-circuit `&&` and `||`
compile to branches before KLEE sees the assumption, which creates extra states
and can silently distort path exploration.

Use these forms:

```cpp
// Conjunction: split into multiple assumptions.
klee_assume(a == 1);
klee_assume(b == 2);

// Disjunction: use bitwise OR on boolean subexpressions.
klee_assume((a == 1) | (b == 2));

// Mixed boolean formula: use bitwise operators inside the expression.
klee_assume(((a == 1) & (b == 2)) | ((c > 3) & (d < 5)));
```

Avoid these forms:

```cpp
klee_assume(a == 1 && b == 2);
klee_assume((a == 1) || (b == 2));
klee_assume((a == 1 && b == 2) || (c > 3 && d < 5));
```

Before replacing `&&`/`||` with `&`/`|`, check that both sides are pure boolean
expressions with no pointer-safety or side-effect dependency. For pointer checks,
split the assumptions instead:

```cpp
klee_assume(p != nullptr);
klee_assume(p->x == 1);
```

Business branches are different: ordinary `if`, `switch`, and error-path
conditions may use normal C++ control flow because those branches are the
behavior KLEE should explore.

## Length Constraints Should Remove Noise

Use `assume_str_len_in` and `assume_repeated_len_in` aggressively to shrink the
state space for symbolic strings and repeated values. Length is often an
implementation detail, not the business signal. The goal is to explore only
length states that can change code behavior.

When choosing candidate lengths, ask: would two lengths lead to different
branches, different output values, or different `call_stub` traces?

- If no, keep one representative length.
- If yes, include the smallest set of representative lengths that exposes those
  behavior differences.
- Do not enumerate every length up to the buffer capacity just because the
  helper permits it.
- Do not use `kStringLen`, repeated capacity, or buffer size as a business rule.

Examples:

- literal equality/non-equality usually needs one length matching the literal
  and, only if relevant, one non-matching representative;
- empty vs non-empty behavior needs `{0, 1}`;
- duplicate/pair logic needs a two-element representative;
- loops over repeated data should constrain the length before the loop, then
  iterate over the constrained effective length.

## Strings

String fields in `core_abi.hpp` and stub POD structs should be plain
`char name[N + 1]` buffers.

- Before the first `kstr_*` operation on a symbolic string, call
  `assume_str_len_in<N>(field)` or `assume_str_len_in<N1, N2>(field)`.
- If you forget, `klee_helpers.hpp` auto-pins the length to `kDefaultStrLen`
  on first `kstr_*` use. That avoids explosion but explores only one length.
- If you call `assume_str_len_in` after a `kstr_*` use, the helper reports a
  `user.err`; move the assumption before the first use.
- Use `kstr_len`, `kstr_eq`, `kstr_eq_lit`, `kstr_cpy`, and `kstr_cat`.
- Prefer `kstr_eq_lit(field, "literal")` for literal comparisons.

## Repeated Values

Use `Repeated<T, N>` only when a variable-length list is genuinely relevant to
the feature behavior. Prefer explicit scalar fields for small fixed shapes.

- Constrain symbolic repeated lengths with
  `assume_repeated_len_in(items, min_len, max_len)`.
- Iterate with `for (uint32_t i = 0; i < items.len; ++i)` after constraining
  the length range.
- Choose representative ranges based on business behavior: `(0, 1)` for
  empty/non-empty, `(1, 1)` for a single representative item, `(2, 2)` for
  pair/duplicate behavior.
- Do not treat a length assumption as a business fact outside solve. It is a
  bounded modeling choice, not a spec requirement.

## `call_stub` Evidence Contract

Use `call_stub` only for a real dependency boundary:

- a function symbol found in the workspace reference code,
- a documented external/system/library boundary,
- or an interface explicitly named by the feature request.

Do not use `call_stub` to invent internal helpers, hide missing `CoreInput`
state, or avoid implementing branch logic. Add a short evidence comment above
each stub naming the real symbol or boundary being modeled.

Each simulation variant should make its own evidence-grounded judgment about
which real dependencies to stub. Different variants may choose different real
dependency boundaries when the feature request is silent; that is a valid L2
signal. Differences caused by invented stub names are not valid ambiguity signals.

Use this shape for stubs:

```cpp
struct SomeBoundaryReq {
    int32_t relevant_field;
};

struct SomeBoundaryResp {
    int32_t result;
    int32_t error;
};

// Evidence: path/to/file.cpp::real_symbol uses this external boundary.
int32_t some_boundary(int32_t arg) {
    SomeBoundaryReq req{arg};
    SomeBoundaryResp resp{};
    call_stub(stub_calls, "real_symbol_or_boundary_name", req, resp);
    return resp.result;
}
```

The method string should be the real symbol or boundary name whenever possible.
If the shared harness omitted state required to implement the feature faithfully,
report it in `notes` / `fuzzy_design_report` instead of hiding the omission
behind a new stub.

## `mock_<i>.cpp` Implementation Contract

- Write a faithful implementation of the feature request as you interpret it.
- Read state modeled by `core_abi.hpp` directly.
- Match each entry-point declaration from `core_abi.hpp` exactly.
- Call local helper functions directly when they are part of your implementation.
- Route only genuine external/repository/system boundaries through `call_stub`.
- Keep all observable data flow inside the shared `core_abi.hpp` structs, local
  POD structs, and `stub_calls`.

If `klee_solve` reports `STUBLOG_OVERFLOW`, add
`#define STUBLOG_BUF_SIZE <doubled_value>` at the very top of `mock_<i>.cpp`, before
`#include "core_abi.hpp"`, then rerun `klee_solve`.

## KLEE Solve Failures

When `klee_solve` fails:

- For compile errors, fix the concrete C++ issue.
- For path explosion, first look for missing string length assumptions,
  symbolic loop bounds, or short-circuit expressions inside `klee_assume`.
- For missing modeled state, report the limitation instead of inventing a stub.
- If you degrade or skip behavior, record it in the final `notes` field.

One-line principle: fewer true paths is better than extra fake paths.
