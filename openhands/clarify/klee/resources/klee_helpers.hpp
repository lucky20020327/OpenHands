// klee_helpers.hpp — KLEE symbolic execution helpers for clarify simulations.
//
// Provides:
//   - StubTrace / call_stub   : TLV-encoded external call recording (static buffer)
//   - assume_str_len_in     : constrain a plain char[N+1] string to exact length n
//   - Repeated<T,N>         : fixed-capacity symbolic array
//   - Non-deterministic stubs: now_sec, rand_int, getenv_or, etc.
//
// String ABI: use plain char str[N+1] fields in CoreInput. Call
// assume_str_len_in<N>(str) before the first kstr_* operation to keep length
// exploration bounded. If not called, the default constraint (kDefaultStrLen=5)
// is applied automatically on first use by ensure_str_budget().
//
// Include this header in both harness.cpp and mock.cpp.
// All klee_make_symbolic calls on CoreInput live in harness.cpp.
// call_stub() makes the Resp symbolic at the call site — no pre-declared sizes needed.
//
// Design note: StubTrace uses a static array, not heap allocation.
// KLEE's symbolic execution does not have real memory pressure, and static
// arrays avoid the pointer-tracking complications of malloc/realloc in KLEE's
// memory model (symbolic bytes in a reallocated buffer require KLEE to copy
// symbolic state across memory objects, which is error-prone).

#pragma once

#ifdef __cplusplus
extern "C" {
#endif
#include <klee/klee.h>
#ifdef __cplusplus
}
#endif

// Replay safety guard: libkleeRuntest replays concrete bytes from KTEST_FILE.
// Any leftover raw klee_assume in generated code must not prune or abort replay
// just because another variant's concrete input violates this variant's solve
// constraints.
#if defined(KLEE_REPLAY)
#ifdef klee_assume
#undef klee_assume
#endif
#define klee_assume(x) ((void)(x))
#endif

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>

// ---------------------------------------------------------------------------
// Tunables (override via -D on clang command line if needed)
// ---------------------------------------------------------------------------

// Byte capacity of the stub trace buffer.
// Static allocation: no heap, no realloc. 1 MiB comfortably fits thousands
// of calls with typical POD structs. KLEE compresses state internally so a
// large static array is fine.
#ifndef STUBLOG_BUF_SIZE
#define STUBLOG_BUF_SIZE (1024 * 1024)
#endif

// Maximum method name length (bytes, excluding null terminator).
#ifndef STUBLOG_MAX_METHOD_LEN
#define STUBLOG_MAX_METHOD_LEN 127
#endif

inline void clarify_report_error(const char* file,
                                 int line,
                                 const char* message,
                                 const char* suffix)
{
#if defined(KLEE_SOLVE) && !defined(KLEE_REPLAY)
    klee_report_error(file, line, message, suffix);
#else
    fprintf(stderr, "KLEE_RUN_TEST_ERROR: %s:%d: %s [%s]\n",
            file, line, message ? message : "", suffix ? suffix : "error");
    abort();
#endif
}

// ---------------------------------------------------------------------------
// TLV-based stub trace (static buffer)
//
// Frame layout:
//   [uint8  method_len ]  1 byte
//   [char*  method     ]  method_len bytes
//   [uint32 req_size   ]  4 bytes
//   [uint8* req        ]  req_size bytes
//   [uint32 resp_size  ]  4 bytes
//   [uint8* resp       ]  resp_size bytes
//
// All pointers and offsets are concrete; only the resp bytes are symbolic.
// KLEE handles writes of symbolic bytes to a concrete static array correctly.
// If the buffer fills, clarify_report_error() terminates the path with an
// "overflow.err" report in solve mode, or aborts with a replay-visible error
// in replay mode. The agent then doubles STUBLOG_BUF_SIZE (via
// -DSTUBLOG_BUF_SIZE=<new> in the compile flags) and retries.
// ---------------------------------------------------------------------------

struct StubTrace {
    uint8_t  buf[STUBLOG_BUF_SIZE];
    uint32_t used;      // bytes written so far
    uint32_t calls;     // number of call_stub() invocations

    StubTrace() : used(0), calls(0) {
        memset(buf, 0, sizeof(buf));
    }

    const uint8_t* data() const { return buf; }
    uint32_t       size() const { return used; }
};

// call_stub — record one external call and symbolize the response.
//
// Req and Resp must be POD types (trivially copyable, no pointers to heap).
// The Resp is made symbolic so KLEE explores all possible return values.
// sizeof(Req) and sizeof(Resp) are resolved at compile time by the template
// instantiation — no pre-declared sizes needed.
template <typename Req, typename Resp>
inline void call_stub(StubTrace&   trace,
                     const char* method,
                     const Req&  req,
                     Resp&       resp)
{
    // Symbolize the response — KLEE will explore all bit patterns.
    klee_make_symbolic(&resp, sizeof(resp), method);

    // Measure method name length (capped at STUBLOG_MAX_METHOD_LEN).
    uint8_t mlen = 0;
    {
        const char* p = method;
        while (*p && mlen < STUBLOG_MAX_METHOD_LEN) { ++p; ++mlen; }
    }

    uint32_t req_size  = static_cast<uint32_t>(sizeof(Req));
    uint32_t resp_size = static_cast<uint32_t>(sizeof(Resp));
    // Frame: [1 mlen][mlen method][4 req_size][req][4 resp_size][resp]
    uint32_t frame_size = 1u + mlen + 4u + req_size + 4u + resp_size;

    if (trace.used + frame_size > STUBLOG_BUF_SIZE) {
        clarify_report_error(__FILE__, __LINE__,
            "STUBLOG_OVERFLOW: stub trace buffer full — double STUBLOG_BUF_SIZE and retry.",
            "overflow.err");
    }

    uint8_t* p = trace.buf + trace.used;

    *p++ = mlen;
    memcpy(p, method,     mlen);      p += mlen;
    memcpy(p, &req_size,  4);         p += 4;
    memcpy(p, &req,       req_size);  p += req_size;
    memcpy(p, &resp_size, 4);         p += 4;
    memcpy(p, &resp,      resp_size);

    trace.used += frame_size;
    ++trace.calls;
}

// ---------------------------------------------------------------------------
// Replay JSON emission
//
// replay_workspace.sh treats a test as replay-successful only if stdout contains
// one line starting with "##REPLAY_JSON## ". Keep the JSON shape fixed here so
// harness_writer only needs to model CoreInput/CoreOutput and call
// emit_replay_json(...).
// ---------------------------------------------------------------------------

namespace replay_detail {

inline const char* b64chars() {
    return "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
}

inline void b64_print(const uint8_t* data, uint32_t n) {
    const char* tab = b64chars();
    uint32_t i = 0;
    while (i + 3 <= n) {
        uint32_t v = (static_cast<uint32_t>(data[i]) << 16) |
                     (static_cast<uint32_t>(data[i + 1]) << 8) |
                     static_cast<uint32_t>(data[i + 2]);
        std::putchar(tab[(v >> 18) & 0x3f]);
        std::putchar(tab[(v >> 12) & 0x3f]);
        std::putchar(tab[(v >> 6) & 0x3f]);
        std::putchar(tab[v & 0x3f]);
        i += 3;
    }
    if (i < n) {
        uint32_t rem = n - i;
        uint32_t v = static_cast<uint32_t>(data[i]) << 16;
        if (rem == 2) v |= static_cast<uint32_t>(data[i + 1]) << 8;
        std::putchar(tab[(v >> 18) & 0x3f]);
        std::putchar(tab[(v >> 12) & 0x3f]);
        std::putchar(rem == 2 ? tab[(v >> 6) & 0x3f] : '=');
        std::putchar('=');
    }
}

inline void json_str_print(const char* s, uint32_t n) {
    for (uint32_t i = 0; i < n; ++i) {
        unsigned char c = static_cast<unsigned char>(s[i]);
        if (c == '"' || c == '\\') {
            std::putchar('\\');
            std::putchar(static_cast<int>(c));
        } else if (c == '\n') {
            std::fputs("\\n", stdout);
        } else if (c == '\r') {
            std::fputs("\\r", stdout);
        } else if (c == '\t') {
            std::fputs("\\t", stdout);
        } else if (c < 0x20) {
            std::printf("\\u%04x", static_cast<unsigned>(c));
        } else {
            std::putchar(static_cast<int>(c));
        }
    }
}

inline uint32_t read_u32(const uint8_t* p) {
    uint32_t v = 0;
    std::memcpy(&v, p, sizeof(v));
    return v;
}

inline void emit_stub_calls_json(const StubTrace& trace) {
    uint32_t off = 0;
    for (uint32_t i = 0; i < trace.calls && off < trace.used; ++i) {
        if (i > 0) std::putchar(',');
        if (off + 1u > trace.used) break;
        uint8_t method_len = trace.buf[off++];
        const char* method = reinterpret_cast<const char*>(trace.buf + off);
        off += method_len;
        if (off + 4u > trace.used) break;
        uint32_t req_size = read_u32(trace.buf + off);
        off += 4u;
        const uint8_t* req = trace.buf + off;
        off += req_size;
        if (off + 4u > trace.used) break;
        uint32_t resp_size = read_u32(trace.buf + off);
        off += 4u;
        const uint8_t* resp = trace.buf + off;
        off += resp_size;

        std::printf("{\"index\":%u,\"method\":\"", static_cast<unsigned>(i));
        json_str_print(method, method_len);
        std::fputs("\",\"request_size\":", stdout);
        std::printf("%u", static_cast<unsigned>(req_size));
        std::fputs(",\"request_bytes_b64\":\"", stdout);
        b64_print(req, req_size);
        std::fputs("\",\"response_size\":", stdout);
        std::printf("%u", static_cast<unsigned>(resp_size));
        std::fputs(",\"response_bytes_b64\":\"", stdout);
        b64_print(resp, resp_size);
        std::fputs("\"}", stdout);
    }
}

}  // namespace replay_detail

template <typename Req, typename Resp>
inline void emit_replay_json(const Req& req,
                             const Resp& resp,
                             int64_t err_code,
                             const StubTrace& trace) {
#if defined(KLEE_REPLAY)
    std::fputs("##REPLAY_JSON## ", stdout);
    std::printf("{\"handler\":{\"req_size\":%u,\"req_bytes_b64\":\"",
                static_cast<unsigned>(sizeof(Req)));
    replay_detail::b64_print(
        reinterpret_cast<const uint8_t*>(&req),
        static_cast<uint32_t>(sizeof(Req)));
    std::fputs("\",\"resp\":{\"err_code\":", stdout);
    std::printf("%lld", static_cast<long long>(err_code));
    std::printf(",\"resp_size\":%u,\"resp_bytes_b64\":\"",
                static_cast<unsigned>(sizeof(Resp)));
    replay_detail::b64_print(
        reinterpret_cast<const uint8_t*>(&resp),
        static_cast<uint32_t>(sizeof(Resp)));
    std::fputs("\"}},\"stub_calls\":[", stdout);
    replay_detail::emit_stub_calls_json(trace);
    std::fputs("],\"stub_trace\":[]}\n", stdout);
    std::fflush(stdout);
#else
    (void)req;
    (void)resp;
    (void)err_code;
    (void)trace;
#endif
}

template <typename Req, typename Result>
inline void emit_replay_json(const Req& req,
                             const Result& result,
                             const StubTrace& trace) {
    emit_replay_json(req, result, static_cast<int64_t>(result), trace);
}

// ---------------------------------------------------------------------------
// String ABI: plain char str[N+1]
//
// Declare string fields in CoreInput and Resp POD structs as:
//   char str[N + 1];   // symbolic bytes; +1 for null terminator
//
// Use klee_make_symbolic normally for the whole struct. Then, BEFORE any
// string operation, call assume_str_len_in to constrain the length:
//
//   klee_make_symbolic(&input, sizeof(input), "input");
//   assume_str_len_in<5>(input.name);           // 1 KLEE path, length = 5
//   assume_str_len_in<3, 5, 8>(input.name);     // 3 KLEE paths, one per length
//
// If assume_str_len_in is NOT called before the first kstr_* use, ensure_str_budget
// fires automatically and pins the string to kDefaultStrLen (=5) — no path explosion,
// but only one length is explored.
//
// Do NOT call strlen / strcmp / strcpy / strcat directly. Use the kstr_* wrappers
// below — they are the only interception points for ensure_str_budget.
// ---------------------------------------------------------------------------

// Default length applied by ensure_str_budget when assume_str_len_in is omitted.
inline constexpr int kDefaultStrLen = 5;

namespace detail {

// side-table: records which string addresses have had their length fixed.
inline constexpr int kMaxStrTags = 4096;
struct StrTag { const void* obj = nullptr; bool auto_pinned = false; };
inline StrTag g_str_tags[kMaxStrTags];
inline int    g_str_tag_n = 0;

inline StrTag* find_str_tag(const void* p) {
    for (int i = 0; i < g_str_tag_n; ++i)
        if (g_str_tags[i].obj == p) return &g_str_tags[i];
    return nullptr;
}
inline bool str_len_fixed(const void* p) { return find_str_tag(p) != nullptr; }

inline void note_str_fixed(const void* p, bool auto_pinned) {
#if defined(KLEE_SOLVE) && !defined(KLEE_REPLAY)
    if (str_len_fixed(p)) return;
    if (g_str_tag_n >= kMaxStrTags) {
        clarify_report_error(__FILE__, __LINE__,
            "klee str-tag table overflow. Increase kMaxStrTags in klee_helpers.hpp.",
            "framework.err");
    }
    g_str_tags[g_str_tag_n].obj         = p;
    g_str_tags[g_str_tag_n].auto_pinned = auto_pinned;
    ++g_str_tag_n;
#else
    (void)p; (void)auto_pinned;
#endif
}

// Exact-length predicate for a single concrete n.
// for(i<n): len_ok &= str[i]; len_ok &= str[n]=='\0'
template <int Cap>
inline int str_has_exact_len(const char (&str)[Cap], int n) {
    if (n < 0) n = 0;
    if (n > Cap - 1) n = Cap - 1;
    int len_ok = 1;
    for (int i = 0; i < n; ++i) len_ok &= (str[i] != '\0');
    len_ok &= (str[n] == '\0');
    return len_ok;
}

template <int Cap>
inline void apply_one_str_len(const char (&str)[Cap], int n) {
    klee_assume(str_has_exact_len(str, n));
}

// Fallback called at every kstr_* entry: pins unconstrained symbolic strings
// to kDefaultStrLen so a forgotten assume_str_len_in never explodes the path space.
template <int Cap>
inline void ensure_str_budget(const char (&str)[Cap]) {
#if defined(KLEE_SOLVE) && !defined(KLEE_REPLAY)
    if (str_len_fixed(str)) return;
    constexpr int n = (kDefaultStrLen < Cap - 1) ? kDefaultStrLen : (Cap - 1);
    apply_one_str_len(str, n);
    note_str_fixed(str, /*auto_pinned=*/true);
#else
    (void)str;
#endif
}

}  // namespace detail

// ---------------------------------------------------------------------------
// assume_str_len_in<N1, N2, ...>(str)
//
// Constrain str to one of the listed lengths before any kstr_* use.
// The allowed length domain is the OR of the listed candidates. This helper
// does not introduce an explicit choice branch; business logic and kstr_*
// operations naturally fork later if they depend on the concrete length.
// Must be called BEFORE the first kstr_* call on this field. Calling after
// ensure_str_budget has already auto-pinned the field triggers user.err.
// ---------------------------------------------------------------------------
template <int... Ns, int Cap>
inline void assume_str_len_in(const char (&str)[Cap]) {
    static_assert(sizeof...(Ns) >= 1,
        "assume_str_len_in requires at least one length argument");
#if defined(KLEE_SOLVE) && !defined(KLEE_REPLAY)
    const detail::StrTag* t = detail::find_str_tag(str);
    if (t != nullptr) {
        clarify_report_error(__FILE__, __LINE__,
            t->auto_pinned
                ? "klee::assume_str_len_in: field was already AUTO-PINNED to "
                  "kDefaultStrLen by an earlier kstr_* call. Move assume_str_len_in "
                  "BEFORE the first kstr_* use on this field."
                : "klee::assume_str_len_in: field length already fixed by a prior "
                  "assume_str_len_in call.",
            "user.err");
    }
    constexpr int count = static_cast<int>(sizeof...(Ns));
    const int ns[] = {Ns...};
    if (count == 1) {
        detail::apply_one_str_len(str, ns[0]);
    } else {
        int allowed = 0;
        for (int i = 0; i < count; ++i)
            allowed |= detail::str_has_exact_len(str, ns[i]);
        klee_assume(allowed);
    }
    detail::note_str_fixed(str, /*auto_pinned=*/false);
#else
    (void)str;
#endif
}

// ---------------------------------------------------------------------------
// kstr_* wrappers — use these instead of strlen / strcmp / strcpy / strcat.
// Each wrapper calls ensure_str_budget on every symbolic string argument,
// providing the interception point for the lazy length-budget fallback.
// ---------------------------------------------------------------------------

// kstr_len: return strlen of str.
template <int Cap>
inline int kstr_len(const char (&str)[Cap]) {
    detail::ensure_str_budget(str);
    for (int i = 0; i < Cap; ++i)
        if (str[i] == '\0') return i;
    return Cap - 1;
}

// kstr_eq: true iff two strings are equal.
template <int CapA, int CapB>
inline bool kstr_eq(const char (&a)[CapA], const char (&b)[CapB]) {
    detail::ensure_str_budget(a);
    detail::ensure_str_budget(b);
    for (int i = 0; i < CapA && i < CapB; ++i) {
        if (a[i] != b[i]) return false;
        if (a[i] == '\0') return true;
    }
    return (CapA == CapB);
}

// kstr_eq_lit: true iff str equals a string literal (literal is concrete).
template <int Cap, int LitN>
inline bool kstr_eq_lit(const char (&str)[Cap], const char (&lit)[LitN]) {
    detail::ensure_str_budget(str);
    for (int i = 0; i < Cap && i < LitN; ++i) {
        if (str[i] != lit[i]) return false;
        if (str[i] == '\0') return true;
    }
    return true;
}

// kstr_cpy: copy src into dst (safe, NUL-terminates dst).
// dst is marked fixed so ensure_str_budget won't re-constrain it later.
template <int DstCap, int SrcCap>
inline void kstr_cpy(char (&dst)[DstCap], const char (&src)[SrcCap]) {
    detail::ensure_str_budget(src);
    int i = 0;
    for (; i < DstCap - 1 && i < SrcCap; ++i) {
        dst[i] = src[i];
        if (src[i] == '\0') {
            detail::note_str_fixed(dst, /*auto_pinned=*/false);
            return;
        }
    }
    dst[i] = '\0';
    detail::note_str_fixed(dst, /*auto_pinned=*/false);
}

// kstr_cat: append src to dst (safe, NUL-terminates dst).
template <int DstCap, int SrcCap>
inline void kstr_cat(char (&dst)[DstCap], const char (&src)[SrcCap]) {
    detail::ensure_str_budget(dst);
    detail::ensure_str_budget(src);
    int dlen = kstr_len(dst);
    for (int i = 0; dlen + i < DstCap - 1 && i < SrcCap; ++i) {
        dst[dlen + i] = src[i];
        if (src[i] == '\0') return;
    }
    dst[DstCap - 1] = '\0';
}

// ---------------------------------------------------------------------------
// Repeated<T, N> — fixed-capacity symbolic array
//
// Advanced: prefer flattening arrays into individual scalar fields when the
// element count is small and fixed. Use Repeated only for genuinely variable-
// length lists where the count itself is part of the spec ambiguity.
// ---------------------------------------------------------------------------

template <typename T, uint32_t N>
struct Repeated {
    T        data[N];
    uint32_t len;  // symbolic element count in [0, N]

    Repeated() : len(0) { memset(data, 0, sizeof(data)); }
};

template <typename T, uint32_t N>
inline void assume_repeated_len_in(Repeated<T, N>& r, uint32_t min_len, uint32_t max_len) {
    klee_assume(r.len >= min_len);
    klee_assume(r.len <= (max_len < N ? max_len : N));
}

// ---------------------------------------------------------------------------
// Non-deterministic environment stubs
// ---------------------------------------------------------------------------

inline int64_t now_sec() {
    int64_t t;
    klee_make_symbolic(&t, sizeof(t), "now_sec");
    klee_assume(t >= 0);
    return t;
}

inline int64_t now_ms() {
    int64_t t;
    klee_make_symbolic(&t, sizeof(t), "now_ms");
    klee_assume(t >= 0);
    return t;
}

inline int32_t rand_int() {
    int32_t v;
    klee_make_symbolic(&v, sizeof(v), "rand_int");
    klee_assume(v >= 0);
    return v;
}

inline const char* getenv_or(const char* /*name*/, const char* default_val) {
    return default_val;
}

inline int32_t fake_pid() {
    int32_t v;
    klee_make_symbolic(&v, sizeof(v), "fake_pid");
    klee_assume(v > 0);
    return v;
}

inline int32_t fake_uid() {
    int32_t v;
    klee_make_symbolic(&v, sizeof(v), "fake_uid");
    klee_assume(v >= 0);
    return v;
}

inline void klee_log(const char* /*msg*/) {}

// ---------------------------------------------------------------------------
// error_code_hash — stable compile-time error code values
//
// Problem: different mock.cpp variants may independently choose the same
// error name (e.g. "ZSTD_error_GENERIC") but assign it different numeric
// values, producing false L3 divergences that are pure LLM noise, not
// genuine spec ambiguity.
//
// Solution: every error code is derived from its name via a constexpr FNV-1a
// hash. Two variants that name the same error always produce the same value;
// two variants that name *different* errors produce different values (with
// overwhelming probability), and that difference is a real divergence signal.
//
// Usage in mock.cpp:
//   static constexpr uint64_t MY_ERROR_FOO = error_code_hash("MY_ERROR_FOO");
//   static constexpr uint64_t MY_ERROR_BAR = error_code_hash("MY_ERROR_BAR");
//
// Rules:
//   - Always pass the same string you use as the constant name.
//   - Never hard-code numeric literals for error codes.
//   - 0 is reserved for success; error_code_hash never returns 0.
// ---------------------------------------------------------------------------

constexpr uint64_t error_code_hash(const char* name) {
    // FNV-1a 64-bit, fully constexpr — KLEE sees only the folded constant.
    uint64_t h = 14695981039346656037ULL;
    for (int i = 0; name[i] != '\0'; ++i) {
        h ^= static_cast<uint64_t>(static_cast<unsigned char>(name[i]));
        h *= 1099511628211ULL;
    }
    // Bit 63 set: guarantees non-zero and keeps values distinct from small
    // integer success codes (0, 1, ...) without truncating the hash space.
    return h | (1ULL << 63);
}

