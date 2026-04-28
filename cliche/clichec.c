/*
 * clichec — fast-fail launcher for cliche-installed CLIs.
 *
 * Reads a cliche cache JSON file (~/.cache/cliche/<pkg>_<dirhash>.json) and
 * services a narrow set of paths without ever spawning a Python interpreter:
 *   - bare-binary / `--help` / `-h`        → top-level command listing
 *   - `--llm-help`                         → full LLM dump (line format)
 *   - `<cmd> --llm-help`                   → per-command LLM dump
 *   - `<group> <cmd> --llm-help`           → per-subcommand LLM dump
 *   - unknown top-level command            → suggestion list, exit 1
 *
 * For everything else (real dispatch, complex --help, --pdb/--pip/--uv/...,
 * stale cache, cache-version mismatch, signatures with pydantic/lazy-arg
 * types, ...) it exits with status 64 — the wrapper script then falls
 * through to the Python launcher (`cliche.launcher:launch_<pkg>`).
 *
 * Usage (from the wrapper):
 *     clichec <cache_file> <pkg_name> [user-args...]
 *
 * No external dependencies. C99, POSIX. ~ stdlib only.
 */

#define _POSIX_C_SOURCE 200809L

#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

#define DEFER 64
#define EXPECTED_CACHE_VERSION "2.2"

/* ============================================================
 *                      ANSI color
 * ============================================================
 *
 * Mirrors run.py's `Colors` / `_supports_color` exactly so output is
 * indistinguishable between the C and Python paths. Same SGR codes
 * (BLUE = "\033[1;36m" — actually bright cyan, kept for parity), same
 * NO_COLOR / FORCE_COLOR override semantics, same "no color when not a
 * TTY" default.
 */
static int color_out = 0;
static int color_err = 0;
#define ANSI_BLUE  "\033[1;36m"
#define ANSI_RED   "\033[1;31m"
#define ANSI_RESET "\033[0m"

static void detect_colors(void) {
    if (getenv("NO_COLOR")) return;
    int force = getenv("FORCE_COLOR") != NULL;
    color_out = force || isatty(STDOUT_FILENO);
    color_err = force || isatty(STDERR_FILENO);
}

static const char *blue_on(int on)  { return on ? ANSI_BLUE  : ""; }
static const char *red_on(int on)   { return on ? ANSI_RED   : ""; }
static const char *reset_on(int on) { return on ? ANSI_RESET : ""; }

/* ============================================================
 *                          arena
 * ============================================================
 *
 * Chunked arena: each chunk is a separate malloc that never moves. Pointers
 * handed out from earlier chunks stay valid for the arena's lifetime, so we
 * can hold pointers into already-allocated regions while continuing to
 * allocate (the previous realloc-grow design corrupted pointers as soon as
 * the cache exceeded the initial 64KB — large CLIs like 1one segfaulted in
 * the JSON parser as a result).
 */

typedef struct ArenaChunk {
    struct ArenaChunk *next;
    size_t cap, used;
    /* data[] follows; intentionally not a flexible array member to keep this
     * portable to older C compilers. We over-allocate per chunk and place
     * the byte buffer directly after this struct. */
} ArenaChunk;

#define CHUNK_BUF(c) ((char *)(c) + sizeof(ArenaChunk))

typedef struct {
    ArenaChunk *head;  /* most-recent chunk; allocations come from here */
} Arena;

static void *arena_alloc(Arena *a, size_t n) {
    n = (n + 7) & ~(size_t)7;
    if (a->head && a->head->cap - a->head->used >= n) {
        char *p = CHUNK_BUF(a->head) + a->head->used;
        a->head->used += n;
        memset(p, 0, n);
        return p;
    }
    size_t chunk_cap = n > 64 * 1024 ? n + 1024 : 64 * 1024;
    ArenaChunk *c = (ArenaChunk *)malloc(sizeof(ArenaChunk) + chunk_cap);
    if (!c) { perror("clichec: malloc"); exit(DEFER); }
    c->next = a->head;
    c->cap  = chunk_cap;
    c->used = n;
    memset(CHUNK_BUF(c), 0, n);
    a->head = c;
    return CHUNK_BUF(c);
}

static void arena_free(Arena *a) {
    ArenaChunk *c = a->head;
    while (c) {
        ArenaChunk *next = c->next;
        free(c);
        c = next;
    }
    a->head = NULL;
}

/* Case-insensitive string equality. Bool-default detection has to match
 * Python's `str(default).lower() == 'true'` semantics: cliche's AST extractor
 * persists True/False with `expr_to_string` which can yield either case
 * depending on how the source spells the literal. Without a case-insensitive
 * compare here, `bool = true` (lowercase, common in Python source via the
 * `bool` shorthand) was being misread as default=False, flipping the
 * `--no-flag` rendering and breaking parity. */
static int str_ieq(const char *a, const char *b) {
    while (*a && *b) {
        char ca = (char)tolower((unsigned char)*a);
        char cb = (char)tolower((unsigned char)*b);
        if (ca != cb) return 0;
        a++; b++;
    }
    return *a == 0 && *b == 0;
}

/* ============================================================
 *                       JSON parser
 * ============================================================ */

enum { JV_NULL, JV_BOOL, JV_NUM, JV_STR, JV_ARR, JV_OBJ };

typedef struct jv {
    int kind;
    union {
        int    b;
        double n;
        struct { const char *s; size_t l; } str;
        struct { struct jv *items; size_t n; } arr;
        struct {
            const char **keys;
            size_t      *klens;
            struct jv   *vals;
            size_t       n;
        } obj;
    } u;
} jv;

typedef struct {
    const char *src;
    size_t      i, len;
    Arena      *a;
    int         err;
} JP;

static void jp_skip_ws(JP *p) {
    while (p->i < p->len) {
        char c = p->src[p->i];
        if (c == ' ' || c == '\t' || c == '\n' || c == '\r') p->i++;
        else break;
    }
}

static int parse_value(JP *p, jv *out);

static int parse_string(JP *p, jv *out) {
    if (p->src[p->i] != '"') { p->err = 1; return -1; }
    p->i++;
    /* In-place decode: strings have no escapes that grow the buffer. We
     * carve from the arena and unescape. */
    size_t start = p->i;
    /* First pass: find end. */
    while (p->i < p->len && p->src[p->i] != '"') {
        if (p->src[p->i] == '\\' && p->i + 1 < p->len) p->i += 2;
        else p->i++;
    }
    if (p->i >= p->len) { p->err = 1; return -1; }
    size_t raw_len = p->i - start;
    char *buf = (char *)arena_alloc(p->a, raw_len + 1);
    size_t bi = 0;
    for (size_t k = start; k < start + raw_len; k++) {
        char c = p->src[k];
        if (c == '\\' && k + 1 < start + raw_len) {
            char n = p->src[k + 1];
            switch (n) {
                case 'n': buf[bi++] = '\n'; break;
                case 't': buf[bi++] = '\t'; break;
                case 'r': buf[bi++] = '\r'; break;
                case 'b': buf[bi++] = '\b'; break;
                case 'f': buf[bi++] = '\f'; break;
                case '"': buf[bi++] = '"';  break;
                case '\\': buf[bi++] = '\\'; break;
                case '/': buf[bi++] = '/'; break;
                case 'u': {
                    /* tiny \uXXXX support: emit ASCII if low, else '?' —
                     * cache strings are practically ASCII so this is plenty. */
                    if (k + 5 >= start + raw_len) { p->err = 1; return -1; }
                    unsigned v = 0;
                    for (int j = 0; j < 4; j++) {
                        char h = p->src[k + 2 + j];
                        v <<= 4;
                        if (h >= '0' && h <= '9') v |= (unsigned)(h - '0');
                        else if (h >= 'a' && h <= 'f') v |= (unsigned)(10 + h - 'a');
                        else if (h >= 'A' && h <= 'F') v |= (unsigned)(10 + h - 'A');
                        else { p->err = 1; return -1; }
                    }
                    if (v < 0x80) buf[bi++] = (char)v;
                    else if (v < 0x800) {
                        buf[bi++] = (char)(0xC0 | (v >> 6));
                        buf[bi++] = (char)(0x80 | (v & 0x3F));
                    } else {
                        buf[bi++] = (char)(0xE0 | (v >> 12));
                        buf[bi++] = (char)(0x80 | ((v >> 6) & 0x3F));
                        buf[bi++] = (char)(0x80 | (v & 0x3F));
                    }
                    k += 4;
                    break;
                }
                default: buf[bi++] = n; break;
            }
            k++;
        } else {
            buf[bi++] = c;
        }
    }
    buf[bi] = 0;
    p->i++; /* closing quote */
    out->kind = JV_STR;
    out->u.str.s = buf;
    out->u.str.l = bi;
    return 0;
}

static int parse_number(JP *p, jv *out) {
    size_t start = p->i;
    if (p->src[p->i] == '-') p->i++;
    while (p->i < p->len) {
        char c = p->src[p->i];
        if ((c >= '0' && c <= '9') || c == '.' || c == 'e' || c == 'E' ||
            c == '+' || c == '-') p->i++;
        else break;
    }
    char tmp[64];
    size_t l = p->i - start;
    if (l >= sizeof(tmp)) { p->err = 1; return -1; }
    memcpy(tmp, p->src + start, l);
    tmp[l] = 0;
    out->kind = JV_NUM;
    out->u.n  = strtod(tmp, NULL);
    return 0;
}

static int parse_array(JP *p, jv *out) {
    p->i++; /* [ */
    jp_skip_ws(p);
    /* growable buffer of jv on the heap, freed once we copy into arena. */
    size_t cap = 4, n = 0;
    jv *items = (jv *)malloc(cap * sizeof(jv));
    if (!items) { p->err = 1; return -1; }
    if (p->i < p->len && p->src[p->i] == ']') {
        p->i++;
        out->kind = JV_ARR;
        out->u.arr.items = NULL;
        out->u.arr.n = 0;
        free(items);
        return 0;
    }
    for (;;) {
        if (n == cap) {
            cap *= 2;
            jv *ni = (jv *)realloc(items, cap * sizeof(jv));
            if (!ni) { free(items); p->err = 1; return -1; }
            items = ni;
        }
        if (parse_value(p, &items[n])) { free(items); return -1; }
        n++;
        jp_skip_ws(p);
        if (p->i >= p->len) { free(items); p->err = 1; return -1; }
        char c = p->src[p->i];
        if (c == ',') { p->i++; jp_skip_ws(p); continue; }
        if (c == ']') { p->i++; break; }
        free(items);
        p->err = 1;
        return -1;
    }
    jv *final = (jv *)arena_alloc(p->a, n * sizeof(jv));
    memcpy(final, items, n * sizeof(jv));
    free(items);
    out->kind = JV_ARR;
    out->u.arr.items = final;
    out->u.arr.n     = n;
    return 0;
}

static int parse_object(JP *p, jv *out) {
    p->i++; /* { */
    jp_skip_ws(p);
    size_t cap = 4, n = 0;
    const char **keys = (const char **)malloc(cap * sizeof(*keys));
    size_t      *klens = (size_t *)malloc(cap * sizeof(*klens));
    jv          *vals  = (jv *)malloc(cap * sizeof(*vals));
    if (!keys || !klens || !vals) {
        free(keys); free(klens); free(vals);
        p->err = 1; return -1;
    }
    if (p->i < p->len && p->src[p->i] == '}') {
        p->i++;
        out->kind = JV_OBJ;
        out->u.obj.keys = NULL;
        out->u.obj.klens = NULL;
        out->u.obj.vals = NULL;
        out->u.obj.n = 0;
        free(keys); free(klens); free(vals);
        return 0;
    }
    for (;;) {
        if (n == cap) {
            cap *= 2;
            keys  = (const char **)realloc(keys, cap * sizeof(*keys));
            klens = (size_t *)realloc(klens, cap * sizeof(*klens));
            vals  = (jv *)realloc(vals, cap * sizeof(*vals));
            if (!keys || !klens || !vals) { p->err = 1; return -1; }
        }
        jp_skip_ws(p);
        jv k;
        if (parse_string(p, &k)) { goto fail; }
        keys[n]  = k.u.str.s;
        klens[n] = k.u.str.l;
        jp_skip_ws(p);
        if (p->i >= p->len || p->src[p->i] != ':') { goto fail; }
        p->i++;
        jp_skip_ws(p);
        if (parse_value(p, &vals[n])) { goto fail; }
        n++;
        jp_skip_ws(p);
        if (p->i >= p->len) { goto fail; }
        char c = p->src[p->i];
        if (c == ',') { p->i++; jp_skip_ws(p); continue; }
        if (c == '}') { p->i++; break; }
        goto fail;
    }
    {
        const char **fk = (const char **)arena_alloc(p->a, n * sizeof(*fk));
        size_t      *fl = (size_t *)arena_alloc(p->a, n * sizeof(*fl));
        jv          *fv = (jv *)arena_alloc(p->a, n * sizeof(*fv));
        memcpy(fk, keys, n * sizeof(*fk));
        memcpy(fl, klens, n * sizeof(*fl));
        memcpy(fv, vals, n * sizeof(*fv));
        free(keys); free(klens); free(vals);
        out->kind = JV_OBJ;
        out->u.obj.keys = fk;
        out->u.obj.klens = fl;
        out->u.obj.vals = fv;
        out->u.obj.n     = n;
    }
    return 0;
fail:
    free(keys); free(klens); free(vals);
    p->err = 1;
    return -1;
}

static int parse_value(JP *p, jv *out) {
    jp_skip_ws(p);
    if (p->i >= p->len) { p->err = 1; return -1; }
    char c = p->src[p->i];
    if (c == '"') return parse_string(p, out);
    if (c == '{') return parse_object(p, out);
    if (c == '[') return parse_array(p, out);
    if (c == 't' && p->i + 4 <= p->len && memcmp(p->src + p->i, "true", 4) == 0) {
        out->kind = JV_BOOL; out->u.b = 1; p->i += 4; return 0;
    }
    if (c == 'f' && p->i + 5 <= p->len && memcmp(p->src + p->i, "false", 5) == 0) {
        out->kind = JV_BOOL; out->u.b = 0; p->i += 5; return 0;
    }
    if (c == 'n' && p->i + 4 <= p->len && memcmp(p->src + p->i, "null", 4) == 0) {
        out->kind = JV_NULL; p->i += 4; return 0;
    }
    if (c == '-' || (c >= '0' && c <= '9')) return parse_number(p, out);
    p->err = 1;
    return -1;
}

/* ===== jv helpers ===== */

static const jv *jv_obj_get(const jv *o, const char *key) {
    if (!o || o->kind != JV_OBJ) return NULL;
    size_t kl = strlen(key);
    for (size_t i = 0; i < o->u.obj.n; i++) {
        if (o->u.obj.klens[i] == kl &&
            memcmp(o->u.obj.keys[i], key, kl) == 0)
            return &o->u.obj.vals[i];
    }
    return NULL;
}

/* ============================================================
 *                  cache → command index
 * ============================================================ */

typedef struct {
    /* User-facing CLI name (underscores→dashes), matches what Python's
     * `build_index` stores in `commands[name]`. Used for both display in
     * help output AND for matching `argv` (since the user types dashes).
     * Mirrors run.py:build_index line 406:
     *     name = func['name'].replace('_', '-')
     * Group names are NOT converted, mirroring Python's behaviour. */
    const char *name;
    const char *group;     /* NULL for ungrouped */
    const jv   *func;      /* full function object from cache */
} CmdEntry;

typedef struct {
    CmdEntry *items;
    size_t    n, cap;
} CmdList;

static void cmdlist_push(CmdList *l, CmdEntry e) {
    if (l->n == l->cap) {
        l->cap = l->cap ? l->cap * 2 : 16;
        l->items = (CmdEntry *)realloc(l->items, l->cap * sizeof(CmdEntry));
        if (!l->items) { perror("clichec: realloc"); exit(DEFER); }
    }
    l->items[l->n++] = e;
}

static int cmd_cmp(const void *a, const void *b) {
    const CmdEntry *x = (const CmdEntry *)a;
    const CmdEntry *y = (const CmdEntry *)b;
    int gc = strcmp(x->group ? x->group : "", y->group ? y->group : "");
    if (gc) return gc;
    return strcmp(x->name, y->name);
}

/* Allocate a copy of `s` (length `l`) in the arena with all '_' → '-'. */
static const char *dasherize(Arena *a, const char *s, size_t l) {
    char *out = (char *)arena_alloc(a, l + 1);
    for (size_t i = 0; i < l; i++) out[i] = (s[i] == '_') ? '-' : s[i];
    out[l] = 0;
    return out;
}

/* Equality treating '_' and '-' as the same character. argparse's command
 * matching treats them interchangeably (so users can type the underscored
 * Python identifier or the canonical dashed CLI form), and run.py:print_help
 * shows the dashed form. We mirror that lenience here so `<cmd> --help`
 * matches regardless of which the user typed. */
static int cmd_name_eq(const char *a, const char *b) {
    while (*a && *b) {
        char ca = (*a == '_') ? '-' : *a;
        char cb = (*b == '_') ? '-' : *b;
        if (ca != cb) return 0;
        a++; b++;
    }
    return *a == 0 && *b == 0;
}

static void build_index(const jv *cache, CmdList *out, Arena *a) {
    const jv *files = jv_obj_get(cache, "files");
    if (!files || files->kind != JV_OBJ) return;
    for (size_t i = 0; i < files->u.obj.n; i++) {
        const jv *finfo = &files->u.obj.vals[i];
        if (finfo->kind != JV_OBJ) continue;
        const jv *fns = jv_obj_get(finfo, "functions");
        if (!fns || fns->kind != JV_ARR) continue;
        for (size_t j = 0; j < fns->u.arr.n; j++) {
            const jv *fn = &fns->u.arr.items[j];
            if (fn->kind != JV_OBJ) continue;
            const jv *name = jv_obj_get(fn, "name");
            if (!name || name->kind != JV_STR) continue;
            const jv *grp = jv_obj_get(fn, "group");
            CmdEntry e = {
                .name  = dasherize(a, name->u.str.s, name->u.str.l),
                .group = (grp && grp->kind == JV_STR) ? grp->u.str.s : NULL,
                .func  = fn,
            };
            cmdlist_push(out, e);
        }
    }
    qsort(out->items, out->n, sizeof(CmdEntry), cmd_cmp);
}

/* ============================================================
 *                   freshness check
 * ============================================================ */

/* Freshness gate: every .py file *and* directory the cache knows about must
 * still exist with the same mtime. py_mtimes catches edits + deletions of
 * tracked files; dir_mtimes catches additions and deletions of untracked
 * files (a new @cli .py landing in an existing dir, a fresh subpackage
 * appearing). runtime.py keeps dir_mtimes in sync on every drift (see the
 * `dirs_drifted` rewrite condition) so a strict equality check here is safe.
 *
 * Without the dir-mtime check clichec would happily serve "Unknown command"
 * for functions defined in brand-new files, since those files are absent from
 * py_mtimes and the wrapper doesn't fall through on rc=1. */
static int cache_is_fresh(const jv *cache, const char *pkg_dir) {
    if (!pkg_dir) return 0;
    const jv *fms = jv_obj_get(cache, "py_mtimes");
    if (!fms || fms->kind != JV_OBJ) return 0;
    char buf[4096];
    for (size_t i = 0; i < fms->u.obj.n; i++) {
        const char *rel = fms->u.obj.keys[i];
        size_t      rl  = fms->u.obj.klens[i];
        const jv   *mv  = &fms->u.obj.vals[i];
        if (mv->kind != JV_NUM) return 0;
        int n = snprintf(buf, sizeof(buf), "%s/%.*s", pkg_dir,
                         (int)rl, rel);
        if (n < 0 || (size_t)n >= sizeof(buf)) return 0;
        struct stat st;
        if (stat(buf, &st) != 0) {
            if (getenv("CLICHEC_DEBUG"))
                fprintf(stderr, "clichec: stat failed for file %s\n", buf);
            return 0;
        }
        double cur = (double)st.st_mtim.tv_sec + st.st_mtim.tv_nsec / 1e9;
        double want = mv->u.n;
        double diff = cur - want;
        if (diff < 0) diff = -diff;
        if (diff > 0.001) {
            if (getenv("CLICHEC_DEBUG"))
                fprintf(stderr, "clichec: file mtime drift %s: cur=%.6f want=%.6f\n",
                        buf, cur, want);
            return 0;
        }
    }

    /* Tracked directories — keys are relative paths ("." for the package
     * root, "sub" for a subpackage, etc.). A drift here indicates a file
     * has been added/removed in that dir, so the cache may not know about
     * a brand-new @cli function. Defer to Python so it can rescan. */
    const jv *dms = jv_obj_get(cache, "dir_mtimes");
    if (dms && dms->kind == JV_OBJ) {
        for (size_t i = 0; i < dms->u.obj.n; i++) {
            const char *rel = dms->u.obj.keys[i];
            size_t      rl  = dms->u.obj.klens[i];
            const jv   *mv  = &dms->u.obj.vals[i];
            if (mv->kind != JV_NUM) return 0;
            int n;
            if (rl == 1 && rel[0] == '.') {
                n = snprintf(buf, sizeof(buf), "%s", pkg_dir);
            } else {
                n = snprintf(buf, sizeof(buf), "%s/%.*s", pkg_dir,
                             (int)rl, rel);
            }
            if (n < 0 || (size_t)n >= sizeof(buf)) return 0;
            struct stat st;
            if (stat(buf, &st) != 0) {
                if (getenv("CLICHEC_DEBUG"))
                    fprintf(stderr, "clichec: stat failed for dir %s\n", buf);
                return 0;
            }
            double cur = (double)st.st_mtim.tv_sec + st.st_mtim.tv_nsec / 1e9;
            double want = mv->u.n;
            double diff = cur - want;
            if (diff < 0) diff = -diff;
            if (diff > 0.001) {
                if (getenv("CLICHEC_DEBUG"))
                    fprintf(stderr, "clichec: dir mtime drift %s: cur=%.6f want=%.6f\n",
                            buf, cur, want);
                return 0;
            }
        }
    }
    return 1;
}

/* ============================================================
 *                 type-annotation classifier
 * ============================================================ */

/* ============================================================
 *                  rendering: --llm-help
 * ============================================================ */

/* Mirror run.py:format_param_llm exactly:
 *   - bool with default → "--name" / "--no-name" (underscore→dash)
 *   - everything else  → "name?:Type=default" (only "?" / ":Type" / "=default"
 *                        when applicable, no spaces)
 * Caller is responsible for separators between params.
 */
static void emit_param_llm_pyparity(const jv *p, FILE *out, const jv *enums) {
    (void)enums;  /* Python's format_param_llm doesn't inline enum choices */
    const jv *name = jv_obj_get(p, "name");
    if (!name || name->kind != JV_STR) return;
    const jv *ann = jv_obj_get(p, "type_annotation");
    const jv *def = jv_obj_get(p, "default");
    int has_def = (def != NULL);

    /* Bool detection: annotation contains "bool", or default is True/False. */
    int is_bool = 0;
    if (ann && ann->kind == JV_STR) {
        for (size_t k = 0; k + 4 <= ann->u.str.l; k++) {
            char c0 = (char)tolower((unsigned char)ann->u.str.s[k]);
            char c1 = (char)tolower((unsigned char)ann->u.str.s[k+1]);
            char c2 = (char)tolower((unsigned char)ann->u.str.s[k+2]);
            char c3 = (char)tolower((unsigned char)ann->u.str.s[k+3]);
            if (c0=='b' && c1=='o' && c2=='o' && c3=='l') { is_bool = 1; break; }
        }
    }
    if (!is_bool && has_def && def->kind == JV_STR) {
        if (str_ieq(def->u.str.s, "True") || str_ieq(def->u.str.s, "False"))
            is_bool = 1;
    }

    if (is_bool && has_def) {
        int default_true = (def->kind == JV_STR && str_ieq(def->u.str.s, "True"));
        fputs(default_true ? "--no-" : "--", out);
        for (size_t k = 0; k < name->u.str.l; k++)
            fputc(name->u.str.s[k] == '_' ? '-' : name->u.str.s[k], out);
        return;
    }

    /* "name?:Type=default" form. */
    fputs(name->u.str.s, out);
    if (has_def) fputc('?', out);
    if (ann && ann->kind == JV_STR) {
        fputc(':', out);
        fputs(ann->u.str.s, out);
    }
    if (has_def && def->kind == JV_STR) {
        fputc('=', out);
        fputs(def->u.str.s, out);
    } else if (has_def && def->kind == JV_NUM) {
        fprintf(out, "=%g", def->u.n);
    }
}

static void emit_function_llm(const jv *fn, FILE *out, const jv *enums) {
    const jv *name   = jv_obj_get(fn, "name");
    const jv *params = jv_obj_get(fn, "parameters");
    if (!name || name->kind != JV_STR) return;
    fputs(name->u.str.s, out);
    fputc('(', out);
    if (params && params->kind == JV_ARR) {
        int first = 1;
        for (size_t i = 0; i < params->u.arr.n; i++) {
            const jv *p = &params->u.arr.items[i];
            if (p->kind != JV_OBJ) continue;
            const jv *pname = jv_obj_get(p, "name");
            if (!pname || pname->kind != JV_STR) continue;
            if (strcmp(pname->u.str.s, "self") == 0 ||
                strcmp(pname->u.str.s, "cls") == 0) continue;
            if (!first) fputs(", ", out);
            first = 0;
            emit_param_llm_pyparity(p, out, enums);
        }
    }
    fputc(')', out);

    /* docstring first-line as " # ..." (matches format_function_llm). */
    const jv *doc = jv_obj_get(fn, "docstring");
    if (doc && doc->kind == JV_STR && doc->u.str.l) {
        const char *s = doc->u.str.s;
        size_t i = 0;
        /* strip leading whitespace to find first non-blank line, like
         * Python's `doc.strip().split('\\n')[0].strip()`. */
        while (s[i] == ' ' || s[i] == '\t' || s[i] == '\n') i++;
        size_t start = i;
        while (s[i] && s[i] != '\n') i++;
        /* trim trailing whitespace */
        size_t end = i;
        while (end > start &&
               (s[end-1] == ' ' || s[end-1] == '\t' || s[end-1] == '\r'))
            end--;
        if (end > start && s[start] != ':') {
            fputs(" # ", out);
            fwrite(s + start, 1, end - start, out);
        }
    }
    fputc('\n', out);
}

static void render_llm_help(const jv *cache, const char *prog, CmdList *cmds) {
    FILE *out = stdout;
    fprintf(out, "# %s CLI - Run: %s <cmd> [args] (space-separated)\n", prog, prog);
    fputs("# Syntax: fn(pos:Type, opt?:Type=default). No ? = positional arg. ? = optional --flag value.\n", out);
    fputs("# Bool flags shown as --flag or --no-flag (use as-is to toggle). Lists/tuples/sets/frozensets: --items a b c.\n", out);
    fputs("# Output: any print() inside the function goes to stdout; a non-None return value is auto-printed.\n", out);
    fprintf(out, "# Per-command detail (full signature, types, defaults, docstrings): %s <cmd> --llm-help  or  %s <group> <cmd> --llm-help\n", prog, prog);
    fputc('\n', out);

    /* ungrouped commands */
    int any_un = 0;
    for (size_t i = 0; i < cmds->n; i++) {
        if (!cmds->items[i].group) { any_un = 1; break; }
    }
    if (any_un) {
        fputs("## commands\n", out);
        for (size_t i = 0; i < cmds->n; i++) {
            if (cmds->items[i].group) continue;
            emit_function_llm(cmds->items[i].func, out, jv_obj_get(cache, "enums"));
        }
        fputc('\n', out);
    }
    /* grouped */
    const char *cur = NULL;
    for (size_t i = 0; i < cmds->n; i++) {
        const CmdEntry *e = &cmds->items[i];
        if (!e->group) continue;
        if (!cur || strcmp(cur, e->group)) {
            if (cur) fputc('\n', out);
            fprintf(out, "## subcommand: %s\n", e->group);
            cur = e->group;
        }
        emit_function_llm(e->func, out, jv_obj_get(cache, "enums"));
    }
    if (cur) fputc('\n', out);

    fputs("## options\n", out);
    fputs("--pdb: Drop into debugger on error\n", out);
    fputs("--pip [args]: Run pip for this CLI's Python env\n", out);
    fputs("--uv [args]: Run uv targeting this CLI's Python env\n", out);
    fputs("--pyspy N: Profile for N seconds with py-spy\n", out);
    fputs("--raw: Print return value as-is (no JSON, no color) — good for pipes\n", out);
    fputs("--notraceback: On error, print only ExcName: message\n", out);
    fputs("--timing: Show timing information\n", out);
    fputs("--skip-gen: Skip cache regeneration\n", out);

    /* enums (compressed) */
    const jv *enums = jv_obj_get(cache, "enums");
    if (enums && enums->kind == JV_OBJ && enums->u.obj.n) {
        fputs("\n## enums\n", out);
        for (size_t i = 0; i < enums->u.obj.n; i++) {
            const jv *vs = &enums->u.obj.vals[i];
            if (vs->kind != JV_ARR) continue;
            fputs(enums->u.obj.keys[i], out);
            fputs(": ", out);
            for (size_t j = 0; j < vs->u.arr.n; j++) {
                if (j) fputc(' ', out);
                const jv *v = &vs->u.arr.items[j];
                if (v->kind == JV_STR) fputs(v->u.str.s, out);
            }
            fputc('\n', out);
        }
    }
}

static int render_command_llm(const jv *cache, const char *prog,
                              const jv *fn, const char *group,
                              const char *cmd) {
    FILE *out = stdout;
    char full[512];
    if (group) snprintf(full, sizeof(full), "%s %s", group, cmd);
    else       snprintf(full, sizeof(full), "%s", cmd);

    fprintf(out, "# %s %s — LLM help\n", prog, full);
    fputs("# Syntax: pos:Type (required positional), opt?:Type=default (use --opt value, underscores->dashes).\n", out);
    fputs("# Bool: --flag to enable (default False) / --no-flag to disable (default True). Lists/tuples/sets/frozensets: space-separated.\n", out);
    /* Description line: must match docstring.py:get_description_without_params
     * → run.py:print_llm_command_help (`first = clean_desc.strip().splitlines()[0].strip()`).
     * The combined effect on a multi-line indented docstring is:
     *   1. Stop at first sphinx/google/numpy section marker (`:param`, `Args:`, etc).
     *   2. Stop at first paragraph break (`\n\n`).
     *   3. Collapse all interior whitespace to single spaces.
     *   4. Print as one `# <text>` line.
     * Without this collapse, an indented multi-line docstring like
     * `"""line one\n    line two."""` would render as just "line one" (cutting
     * at the newline) — drifting from Python which renders the joined form. */
    const jv *doc = jv_obj_get(fn, "docstring");
    if (doc && doc->kind == JV_STR && doc->u.str.l) {
        const char *s = doc->u.str.s;
        size_t end = doc->u.str.l;
        /* Sphinx section markers — string-search (not regex). */
        static const char *sphinx_marks[] = {
            ":param", ":return", ":raises", ":type", ":rtype", NULL
        };
        for (int i = 0; sphinx_marks[i]; i++) {
            const char *p = strstr(s, sphinx_marks[i]);
            if (p) {
                size_t off = (size_t)(p - s);
                if (off < end) end = off;
            }
        }
        /* Google/Numpy section markers (`\n[ws]*Args:` etc). */
        static const char *gn_marks[] = {
            "Args:", "Arguments:", "Parameters:", "Returns:", "Return:",
            "Raises:", "Yields:", "Examples:", "Example:",
            "Note:", "Notes:", "Attributes:", NULL
        };
        for (int i = 0; gn_marks[i]; i++) {
            const char *p = strstr(s, gn_marks[i]);
            while (p) {
                size_t off = (size_t)(p - s);
                /* must be at start of line (preceded by '\n' + optional ws) */
                int at_line_start = (off == 0);
                size_t k = off;
                while (k > 0 && (s[k-1] == ' ' || s[k-1] == '\t')) k--;
                if (k == 0 || s[k-1] == '\n') at_line_start = 1;
                if (at_line_start && off < end) { end = off; break; }
                p = strstr(p + 1, gn_marks[i]);
            }
        }
        /* Paragraph break ends the description. */
        const char *par = strstr(s, "\n\n");
        if (par) {
            size_t off = (size_t)(par - s);
            if (off < end) end = off;
        }
        /* Strip leading whitespace + collapse interior whitespace runs. */
        size_t start = 0;
        while (start < end && (s[start] == ' ' || s[start] == '\t' ||
                               s[start] == '\n' || s[start] == '\r'))
            start++;
        if (end > start) {
            fputs("# ", out);
            int prev_ws = 0, wrote_any = 0;
            for (size_t i = start; i < end; i++) {
                char c = s[i];
                int is_ws = (c == ' ' || c == '\t' || c == '\n' || c == '\r');
                if (is_ws) {
                    prev_ws = 1;
                } else {
                    if (prev_ws && wrote_any) fputc(' ', out);
                    fputc(c, out);
                    prev_ws = 0;
                    wrote_any = 1;
                }
            }
            fputc('\n', out);
        }
    }

    /* usage line */
    const jv *params = jv_obj_get(fn, "parameters");
    fprintf(out, "usage: %s %s", prog, full);
    if (params && params->kind == JV_ARR) {
        /* positionals first */
        for (size_t i = 0; i < params->u.arr.n; i++) {
            const jv *p = &params->u.arr.items[i];
            if (p->kind != JV_OBJ) continue;
            const jv *pname = jv_obj_get(p, "name");
            const jv *pdef  = jv_obj_get(p, "default");
            if (!pname || pname->kind != JV_STR) continue;
            if (pdef) continue;
            const jv *isa = jv_obj_get(p, "is_args");
            const jv *isk = jv_obj_get(p, "is_kwargs");
            if ((isa && isa->kind == JV_BOOL && isa->u.b) ||
                (isk && isk->kind == JV_BOOL && isk->u.b)) continue;
            if (strcmp(pname->u.str.s, "self") == 0 ||
                strcmp(pname->u.str.s, "cls") == 0) continue;
            fputc(' ', out);
            for (const char *c = pname->u.str.s; *c; c++) fputc(toupper((unsigned char)*c), out);
        }
        for (size_t i = 0; i < params->u.arr.n; i++) {
            const jv *p = &params->u.arr.items[i];
            if (p->kind != JV_OBJ) continue;
            const jv *pname = jv_obj_get(p, "name");
            const jv *pdef  = jv_obj_get(p, "default");
            if (!pname || pname->kind != JV_STR) continue;
            if (!pdef) continue;
            const jv *ann   = jv_obj_get(p, "type_annotation");
            int is_bool = (ann && ann->kind == JV_STR && strstr(ann->u.str.s, "bool")) ||
                          (pdef->kind == JV_STR &&
                           (str_ieq(pdef->u.str.s, "True") ||
                            str_ieq(pdef->u.str.s, "False")));
            char flag[256];
            size_t fi = 0;
            for (const char *c = pname->u.str.s; *c && fi + 1 < sizeof(flag); c++) {
                flag[fi++] = (*c == '_') ? '-' : *c;
            }
            flag[fi] = 0;
            if (is_bool) {
                int default_true = (pdef->kind == JV_STR &&
                                    str_ieq(pdef->u.str.s, "True"));
                fprintf(out, " [--%s%s]", default_true ? "no-" : "", flag);
            } else {
                fprintf(out, " [--%s VAL]", flag);
            }
        }
    }
    fputc('\n', out);
    fputc('\n', out);

    if (params && params->kind == JV_ARR) {
        int any_pos = 0, any_opt = 0;
        for (size_t i = 0; i < params->u.arr.n; i++) {
            const jv *p = &params->u.arr.items[i];
            if (p->kind != JV_OBJ) continue;
            const jv *pname = jv_obj_get(p, "name");
            if (!pname || pname->kind != JV_STR) continue;
            if (strcmp(pname->u.str.s, "self") == 0 ||
                strcmp(pname->u.str.s, "cls") == 0) continue;
            const jv *isa = jv_obj_get(p, "is_args");
            const jv *isk = jv_obj_get(p, "is_kwargs");
            if ((isa && isa->kind == JV_BOOL && isa->u.b) ||
                (isk && isk->kind == JV_BOOL && isk->u.b)) continue;
            if (jv_obj_get(p, "default")) any_opt = 1;
            else any_pos = 1;
        }
        if (any_pos) {
            fputs("## positional (required, pass values directly)\n", out);
            for (size_t i = 0; i < params->u.arr.n; i++) {
                const jv *p = &params->u.arr.items[i];
                if (p->kind != JV_OBJ) continue;
                if (jv_obj_get(p, "default")) continue;
                const jv *isa = jv_obj_get(p, "is_args");
                const jv *isk = jv_obj_get(p, "is_kwargs");
                if ((isa && isa->kind == JV_BOOL && isa->u.b) ||
                    (isk && isk->kind == JV_BOOL && isk->u.b)) continue;
                emit_param_llm_pyparity(p, out, jv_obj_get(cache, "enums"));
                fputc('\n', out);
            }
            fputc('\n', out);
        }
        if (any_opt) {
            fputs("## optional (flags)\n", out);
            for (size_t i = 0; i < params->u.arr.n; i++) {
                const jv *p = &params->u.arr.items[i];
                if (p->kind != JV_OBJ) continue;
                if (!jv_obj_get(p, "default")) continue;
                const jv *isa = jv_obj_get(p, "is_args");
                const jv *isk = jv_obj_get(p, "is_kwargs");
                if ((isa && isa->kind == JV_BOOL && isa->u.b) ||
                    (isk && isk->kind == JV_BOOL && isk->u.b)) continue;
                emit_param_llm_pyparity(p, out, jv_obj_get(cache, "enums"));
                fputc('\n', out);
            }
            fputc('\n', out);
        }
    }

    /* The four trailing lines below are copied verbatim from
     * run.py:print_llm_command_help (the global options + the top-level-only
     * note). Parity test (tests/test_clichec_parity.py) keeps them in lock-step. */
    fputs("## global options\n", out);
    fputs("--pdb: debugger on error | --pyspy N: profile Ns | --raw: plain output (no JSON/color)\n", out);
    fputs("--notraceback: terse errors | --timing: timing info | --llm-help: this view\n", out);
    fprintf(out, "# Top-level only (run on `%s` itself): --version, --cli, --pip, --uv, --skip-gen — see `%s --llm-help`\n",
            prog, prog);
    return 0;
}

/* ============================================================
 *                 rendering: top-level --help
 * ============================================================ */

static const char *first_doc_line(const jv *fn) {
    const jv *d = jv_obj_get(fn, "docstring");
    if (!d || d->kind != JV_STR) return NULL;
    return d->u.str.s; /* caller stops at \n */
}

static void render_top_help(const char *prog, CmdList *cmds) {
    FILE *out = stdout;
    const char *B = blue_on(color_out), *R = reset_on(color_out);
    fprintf(out,
        "%susage: %s [-h] [--llm-help] [--pdb] [--pip] [--uv] [--pyspy N] [--timing] COMMAND ...%s\n\n",
        B, prog, R);
    fputs("COMMANDS:\n", out);
    for (size_t i = 0; i < cmds->n; i++) {
        if (cmds->items[i].group) continue;
        const char *doc = first_doc_line(cmds->items[i].func);
        if (doc) {
            /* Pad name to 20 chars (matches Python's `f"    {name:20}"`),
             * colour only the padded-name span — docstring stays uncoloured. */
            int nlen = (int)strlen(cmds->items[i].name);
            fprintf(out, "%s    %s%*s%s", B, cmds->items[i].name,
                    nlen < 20 ? 20 - nlen : 0, "", R);
            /* `doc[:50]` in Python truncates to 50 *code points*, not bytes.
             * Walking the UTF-8 by counting only lead bytes (top bits != 10)
             * matches Python's slicing for any docstring containing multi-byte
             * characters like the em-dash in `cliche_test/cli.py:echo_dict_str`. */
            size_t k = 0, cp = 0;
            while (doc[k] && doc[k] != '\n') {
                unsigned char b = (unsigned char)doc[k];
                if ((b & 0xC0) != 0x80) {
                    if (cp >= 50) break;
                    cp++;
                }
                fputc((char)b, out);
                k++;
            }
            fputc('\n', out);
        } else {
            /* Python uses `padded_name.rstrip()` when no doc — colour the
             * rstripped name (no trailing spaces). */
            fprintf(out, "%s    %s%s\n", B, cmds->items[i].name, R);
        }
    }
    fputs("\nSUBCOMMANDS:\n", out);
    const char *cur = NULL;
    int first_in_group = 1;
    for (size_t i = 0; i < cmds->n; i++) {
        const CmdEntry *e = &cmds->items[i];
        if (!e->group) continue;
        if (!cur || strcmp(cur, e->group)) {
            if (cur) fputs(")\n", out);
            int glen = (int)strlen(e->group);
            fprintf(out, "%s    %s%*s%s(", B, e->group,
                    glen < 16 ? 16 - glen : 0, "", R);
            cur = e->group;
            first_in_group = 1;
        }
        if (!first_in_group) fputs(", ", out);
        fputs(e->name, out);
        first_in_group = 0;
    }
    if (cur) fputs(")\n", out);

    /* Strings copied verbatim from run.py:print_help so the parity test
     * (tests/test_clichec_parity.py) catches drift. The irregular alignment
     * on the `--llm-help` line (9 spaces of padding, not the canonical 4)
     * is a faithful copy of Python's output, NOT a typo. */
    fputs("\nCLICHE OPTIONS:\n", out);
    fprintf(out, "  %s-h%s, %s--help%s    Show this help message\n", B,R,B,R);
    fprintf(out, "  %s--version%s     Print the package version and exit\n", B,R);
    fprintf(out, "  %s--cli%s         Show CLI and Python version info (including package version)\n", B,R);
    fprintf(out, "  %s--llm-help%s         Show compact LLM-friendly help output\n", B,R);
    fprintf(out, "  %s--pdb%s         Drop into debugger on error\n", B,R);
    fprintf(out, "  %s--pip%s         Run pip for this CLI's Python environment\n", B,R);
    fprintf(out, "  %s--uv%s          Run uv targeting this CLI's Python environment\n", B,R);
    fprintf(out, "  %s--pyspy N%s     Profile for N seconds with py-spy (speedscope format)\n", B,R);
    fprintf(out, "  %s--raw%s         Print return value as-is (no JSON, no color)\n", B,R);
    fprintf(out, "  %s--notraceback%s On error, print only ExcName: message\n", B,R);
    fprintf(out, "  %s--skip-gen%s    Skip cache regeneration\n", B,R);
    fprintf(out, "  %s--timing%s      Show timing information\n", B,R);
}

/* ============================================================
 *           rendering: per-command --help (argparse-style)
 * ============================================================
 *
 * Mirrors `<cmd> --help` from run.py closely enough to be drop-in for the
 * tab-complete + skim-help workflow. Defers (returns DEFER) when the
 * function references a pydantic model — those need per-field expansion
 * that lives on the Python side. lazy_arg defaults render as "Default:
 * <source-text>"; the user gets the same readable surface as Python.
 */

/* Compute short flag for each param, matching cliche/abbrev.py exactly:
 * first letter lowercase, then uppercase, otherwise none. -h/-H reserved.
 * Writes parallel arrays into the arena; index matches param order. */
static void compute_short_flags(const jv *params, char ***out_short, Arena *a) {
    if (!params || params->kind != JV_ARR) {
        *out_short = NULL;
        return;
    }
    char **arr = (char **)arena_alloc(a, sizeof(char *) * params->u.arr.n);
    /* used set: -h, -H, plus whatever we hand out */
    char used[256] = {0};
    used[(unsigned char)'h'] = 1;
    used[(unsigned char)'H'] = 1;
    for (size_t i = 0; i < params->u.arr.n; i++) {
        arr[i] = NULL;
        const jv *p = &params->u.arr.items[i];
        if (p->kind != JV_OBJ) continue;
        const jv *def = jv_obj_get(p, "default");
        if (!def) continue;  /* positional → no short */
        const jv *isa = jv_obj_get(p, "is_args");
        const jv *isk = jv_obj_get(p, "is_kwargs");
        if ((isa && isa->kind == JV_BOOL && isa->u.b) ||
            (isk && isk->kind == JV_BOOL && isk->u.b)) continue;
        const jv *pn = jv_obj_get(p, "name");
        if (!pn || pn->kind != JV_STR || !pn->u.str.l) continue;
        unsigned char first = (unsigned char)pn->u.str.s[0];
        unsigned char upper = (unsigned char)((first >= 'a' && first <= 'z')
                                              ? first - 'a' + 'A' : first);
        unsigned char lower = (unsigned char)((first >= 'A' && first <= 'Z')
                                              ? first - 'A' + 'a' : first);
        unsigned char pick = 0;
        if (!used[lower]) pick = lower;
        else if (!used[upper]) pick = upper;
        if (pick) {
            used[pick] = 1;
            char *s = (char *)arena_alloc(a, 3);
            s[0] = '-'; s[1] = (char)pick; s[2] = 0;
            arr[i] = s;
        }
    }
    *out_short = arr;
}

/* Lookup an enum's value list by annotation head ("Color" out of "Color"
 * or "Color | None"). Returns the JV array or NULL. */
static const jv *enum_for_annotation(const jv *enums, const char *ann) {
    if (!ann || !enums || enums->kind != JV_OBJ) return NULL;
    /* Take leading identifier */
    size_t i = 0;
    while (ann[i] && (isalnum((unsigned char)ann[i]) || ann[i] == '_')) i++;
    if (!i) return NULL;
    for (size_t k = 0; k < enums->u.obj.n; k++) {
        if (enums->u.obj.klens[k] == i &&
            memcmp(enums->u.obj.keys[k], ann, i) == 0) {
            return &enums->u.obj.vals[k];
        }
    }
    return NULL;
}

/* True if any param annotation references a pydantic model name from the
 * cache. We can't expand pydantic fields without Python; defer when we see
 * one so the user gets the canonical Python --help, fields and all. */
static int touches_pydantic(const jv *params, const jv *pyd_models) {
    if (!params || params->kind != JV_ARR) return 0;
    if (!pyd_models || pyd_models->kind != JV_ARR || !pyd_models->u.arr.n) return 0;
    for (size_t i = 0; i < params->u.arr.n; i++) {
        const jv *p = &params->u.arr.items[i];
        if (p->kind != JV_OBJ) continue;
        const jv *ann = jv_obj_get(p, "type_annotation");
        if (!ann || ann->kind != JV_STR) continue;
        for (size_t k = 0; k < pyd_models->u.arr.n; k++) {
            const jv *m = &pyd_models->u.arr.items[k];
            if (m->kind != JV_STR) continue;
            if (strstr(ann->u.str.s, m->u.str.s)) return 1;
        }
    }
    return 0;
}

/* Render `{a,b,c}` choices for an enum-typed param, optionally coloured. */
static void emit_choices(FILE *out, const jv *enum_vals, int colored) {
    const char *B = blue_on(colored), *R = reset_on(colored);
    fprintf(out, "%s{", B);
    for (size_t i = 0; i < enum_vals->u.arr.n; i++) {
        const jv *v = &enum_vals->u.arr.items[i];
        if (i) fputc(',', out);
        if (v->kind == JV_STR) fputs(v->u.str.s, out);
    }
    fprintf(out, "}%s", R);
}

static int render_command_help(const jv *cache, const char *prog,
                               const jv *fn, const char *group,
                               const char *cmd, Arena *a) {
    const jv *params = jv_obj_get(fn, "parameters");
    const jv *pyd    = jv_obj_get(cache, "pydantic_models");
    if (touches_pydantic(params, pyd)) return DEFER;
    const jv *enums  = jv_obj_get(cache, "enums");

    char **shorts = NULL;
    compute_short_flags(params, &shorts, a);

    FILE *out = stdout;
    const char *B = blue_on(color_out), *R = reset_on(color_out);
    char full[512];
    if (group) snprintf(full, sizeof(full), "%s %s", group, cmd);
    else       snprintf(full, sizeof(full), "%s", cmd);

    /* usage line — coloured as a whole, like run.py's colorize_help. */
    fprintf(out, "%susage: %s %s [-h] [--llm-help] [--pdb] [--pyspy N] "
                 "[--raw] [--notraceback] [--timing]", B, prog, full);
    /* optional flags (alpha order isn't strictly required; param order is fine) */
    if (params && params->kind == JV_ARR) {
        for (size_t i = 0; i < params->u.arr.n; i++) {
            const jv *p = &params->u.arr.items[i];
            if (p->kind != JV_OBJ) continue;
            const jv *pn = jv_obj_get(p, "name");
            const jv *pd = jv_obj_get(p, "default");
            if (!pn || pn->kind != JV_STR || !pd) continue;
            const jv *isa = jv_obj_get(p, "is_args");
            const jv *isk = jv_obj_get(p, "is_kwargs");
            if ((isa && isa->kind == JV_BOOL && isa->u.b) ||
                (isk && isk->kind == JV_BOOL && isk->u.b)) continue;
            const jv *ann = jv_obj_get(p, "type_annotation");
            int is_bool = (ann && ann->kind == JV_STR && strstr(ann->u.str.s, "bool"))
                         || (pd->kind == JV_STR &&
                             (str_ieq(pd->u.str.s, "True") ||
                              str_ieq(pd->u.str.s, "False")));
            int default_true = (pd->kind == JV_STR && str_ieq(pd->u.str.s, "True"));
            char dashed[256];
            size_t fi = 0;
            for (size_t k = 0; k < pn->u.str.l && fi + 1 < sizeof(dashed); k++) {
                dashed[fi++] = (pn->u.str.s[k] == '_') ? '-' : pn->u.str.s[k];
            }
            dashed[fi] = 0;
            if (is_bool) {
                fprintf(out, " [--%s%s]", default_true ? "no-" : "", dashed);
            } else if (shorts && shorts[i]) {
                /* uppercase metavar from the param name */
                char meta[64];
                size_t ml = pn->u.str.l < sizeof(meta) - 1 ? pn->u.str.l : sizeof(meta) - 1;
                for (size_t k = 0; k < ml; k++) meta[k] = (char)toupper((unsigned char)pn->u.str.s[k]);
                meta[ml] = 0;
                fprintf(out, " [%s %s]", shorts[i], meta);
            } else {
                char meta[64];
                size_t ml = pn->u.str.l < sizeof(meta) - 1 ? pn->u.str.l : sizeof(meta) - 1;
                for (size_t k = 0; k < ml; k++) meta[k] = (char)toupper((unsigned char)pn->u.str.s[k]);
                meta[ml] = 0;
                fprintf(out, " [--%s %s]", dashed, meta);
            }
        }
        /* positionals last */
        for (size_t i = 0; i < params->u.arr.n; i++) {
            const jv *p = &params->u.arr.items[i];
            if (p->kind != JV_OBJ) continue;
            const jv *pn = jv_obj_get(p, "name");
            const jv *pd = jv_obj_get(p, "default");
            if (!pn || pn->kind != JV_STR || pd) continue;
            const jv *isa = jv_obj_get(p, "is_args");
            const jv *isk = jv_obj_get(p, "is_kwargs");
            if ((isa && isa->kind == JV_BOOL && isa->u.b) ||
                (isk && isk->kind == JV_BOOL && isk->u.b)) continue;
            char meta[64];
            size_t ml = pn->u.str.l < sizeof(meta) - 1 ? pn->u.str.l : sizeof(meta) - 1;
            for (size_t k = 0; k < ml; k++) meta[k] = (char)toupper((unsigned char)pn->u.str.s[k]);
            meta[ml] = 0;
            fprintf(out, " %s", meta);
        }
    }
    fprintf(out, "%s\n\n", R);

    /* docstring (clean: strip param description lines starting with `:param` etc.) */
    const jv *doc = jv_obj_get(fn, "docstring");
    if (doc && doc->kind == JV_STR && doc->u.str.l) {
        const char *s = doc->u.str.s;
        size_t i = 0;
        while (s[i]) {
            /* skip lines that look like param descriptors */
            size_t line_start = i;
            while (s[i] && s[i] != '\n') i++;
            size_t line_end = i;
            const char *ln = s + line_start;
            size_t ll = line_end - line_start;
            /* trim leading ws */
            size_t lead = 0;
            while (lead < ll && (ln[lead] == ' ' || ln[lead] == '\t')) lead++;
            int is_param_line = 0;
            if (ll - lead >= 7 &&
                (memcmp(ln + lead, ":param ", 7) == 0 ||
                 memcmp(ln + lead, ":return", 7) == 0 ||
                 memcmp(ln + lead, ":raises", 7) == 0 ||
                 memcmp(ln + lead, ":rtype:", 7) == 0))
                is_param_line = 1;
            if (!is_param_line) {
                fwrite(ln, 1, ll, out);
                fputc('\n', out);
            }
            if (s[i] == '\n') i++;
        }
        fputc('\n', out);
    }

    /* positional + optional sections */
    int any_pos = 0, any_opt = 0;
    if (params && params->kind == JV_ARR) {
        for (size_t i = 0; i < params->u.arr.n; i++) {
            const jv *p = &params->u.arr.items[i];
            if (p->kind != JV_OBJ) continue;
            const jv *isa = jv_obj_get(p, "is_args");
            const jv *isk = jv_obj_get(p, "is_kwargs");
            if ((isa && isa->kind == JV_BOOL && isa->u.b) ||
                (isk && isk->kind == JV_BOOL && isk->u.b)) continue;
            if (jv_obj_get(p, "default")) any_opt = 1;
            else any_pos = 1;
        }
    }
    if (any_pos) {
        fputs("POSITIONAL ARGUMENTS:\n", out);
        for (size_t i = 0; i < params->u.arr.n; i++) {
            const jv *p = &params->u.arr.items[i];
            if (p->kind != JV_OBJ) continue;
            const jv *pn = jv_obj_get(p, "name");
            const jv *pd = jv_obj_get(p, "default");
            if (!pn || pn->kind != JV_STR || pd) continue;
            const jv *isa = jv_obj_get(p, "is_args");
            const jv *isk = jv_obj_get(p, "is_kwargs");
            if ((isa && isa->kind == JV_BOOL && isa->u.b) ||
                (isk && isk->kind == JV_BOOL && isk->u.b)) continue;
            const jv *ann = jv_obj_get(p, "type_annotation");
            const jv *evals = ann && ann->kind == JV_STR
                              ? enum_for_annotation(enums, ann->u.str.s) : NULL;
            fputs("  ", out);
            if (evals && evals->kind == JV_ARR && evals->u.arr.n) {
                emit_choices(out, evals, color_out);
                fputc('\n', out);
                fputs("                        ", out);
            }
            if (ann && ann->kind == JV_STR) {
                fprintf(out, "|%s|", ann->u.str.s);
            } else {
                fputs(pn->u.str.s, out);
            }
            fputc('\n', out);
        }
    }
    if (any_opt) {
        if (any_pos) fputc('\n', out);
        fputs("OPTIONS:\n", out);
        for (size_t i = 0; i < params->u.arr.n; i++) {
            const jv *p = &params->u.arr.items[i];
            if (p->kind != JV_OBJ) continue;
            const jv *pn = jv_obj_get(p, "name");
            const jv *pd = jv_obj_get(p, "default");
            if (!pn || pn->kind != JV_STR || !pd) continue;
            const jv *isa = jv_obj_get(p, "is_args");
            const jv *isk = jv_obj_get(p, "is_kwargs");
            if ((isa && isa->kind == JV_BOOL && isa->u.b) ||
                (isk && isk->kind == JV_BOOL && isk->u.b)) continue;
            const jv *ann = jv_obj_get(p, "type_annotation");
            int is_bool = (ann && ann->kind == JV_STR && strstr(ann->u.str.s, "bool"))
                         || (pd->kind == JV_STR &&
                             (str_ieq(pd->u.str.s, "True") ||
                              str_ieq(pd->u.str.s, "False")));
            int default_true = (pd->kind == JV_STR && str_ieq(pd->u.str.s, "True"));
            char dashed[256];
            size_t fi = 0;
            for (size_t k = 0; k < pn->u.str.l && fi + 1 < sizeof(dashed); k++) {
                dashed[fi++] = (pn->u.str.s[k] == '_') ? '-' : pn->u.str.s[k];
            }
            dashed[fi] = 0;

            fputs("  ", out);
            if (is_bool) {
                fprintf(out, "%s--%s%s%s",
                        B, default_true ? "no-" : "", dashed, R);
            } else {
                if (shorts && shorts[i]) {
                    fprintf(out, "%s%s%s, ", B, shorts[i], R);
                }
                fprintf(out, "%s--%s%s ", B, dashed, R);
                const jv *evals = ann && ann->kind == JV_STR
                                  ? enum_for_annotation(enums, ann->u.str.s) : NULL;
                if (evals && evals->kind == JV_ARR && evals->u.arr.n) {
                    emit_choices(out, evals, color_out);
                } else {
                    /* uppercase metavar */
                    for (size_t k = 0; k < pn->u.str.l; k++)
                        fputc(toupper((unsigned char)pn->u.str.s[k]), out);
                }
            }
            fputc('\n', out);
            fputs("                        ", out);
            if (ann && ann->kind == JV_STR) {
                fprintf(out, "|%s|", ann->u.str.s);
                if (!is_bool) fputc(' ', out);
            }
            if (!is_bool) {
                /* default rendering */
                fprintf(out, "%sDefault: ", B);
                if (pd->kind == JV_STR) fputs(pd->u.str.s, out);
                else if (pd->kind == JV_NUM) fprintf(out, "%g", pd->u.n);
                else if (pd->kind == JV_BOOL) fputs(pd->u.b ? "True" : "False", out);
                else if (pd->kind == JV_NULL) fputs("None", out);
                fprintf(out, " |%s", R);
            }
            fputc('\n', out);
        }
    }

    fputs("\nCLICHE OPTIONS:\n", out);
    fprintf(out, "  %s-h%s, %s--help%s            Show this help message\n", B,R,B,R);
    fprintf(out, "  %s--llm-help%s            Show this command's compact LLM-friendly help\n", B,R);
    fprintf(out, "  %s--pdb%s                 Drop into debugger on error\n", B,R);
    fprintf(out, "  %s--pyspy N%s             Profile for N seconds with py-spy\n", B,R);
    fprintf(out, "  %s--raw%s                 Print return value as-is\n", B,R);
    fprintf(out, "  %s--notraceback%s         On error, print only ExcName: message\n", B,R);
    fprintf(out, "  %s--timing%s              Show timing information\n", B,R);
    return 0;
}

/* ============================================================
 *                     unknown-command output
 * ============================================================
 *
 * Output format mirrors run.py exactly:
 *     `Unknown command: <dasherized-bad>`
 *     `Did you mean: <prog> [group] <name>?`     (only when a close match exists)
 *
 * The Levenshtein algorithm and threshold below are duplicated in
 * cliche/run.py:_levenshtein/_suggest_command; the parity test
 * (tests/test_clichec_parity.py) keeps the two implementations in lockstep.
 * If you change one, change the other in the same commit. */

/* Case-insensitive edit distance, capped at 32 chars on both inputs. The
 * cap matches run.py — commands are short identifiers, anything longer is
 * almost certainly not a typo and we'd rather skip the O(la*lb) DP than
 * pay it on a stray paste of a long string. */
static int lev(const char *a, const char *b) {
    size_t la = strlen(a), lb = strlen(b);
    if (la > 32 || lb > 32) return (int)(la > lb ? la : lb);
    int prev[33], curr[33];
    for (size_t j = 0; j <= lb; j++) prev[j] = (int)j;
    for (size_t i = 1; i <= la; i++) {
        curr[0] = (int)i;
        for (size_t j = 1; j <= lb; j++) {
            int cost = (tolower((unsigned char)a[i-1]) ==
                        tolower((unsigned char)b[j-1])) ? 0 : 1;
            int del = prev[j] + 1;
            int ins = curr[j-1] + 1;
            int sub = prev[j-1] + cost;
            int m = del < ins ? del : ins;
            if (sub < m) m = sub;
            curr[j] = m;
        }
        memcpy(prev, curr, sizeof(int) * (lb + 1));
    }
    return prev[lb];
}

static void unknown_command(const char *prog, const char *bad,
                            CmdList *cmds, Arena *a) {
    const char *bad_dashed = dasherize(a, bad, strlen(bad));
    fprintf(stderr, "Unknown command: %s\n", bad_dashed);

    /* Find the closest match across every (top-level cmd ∪ subcommand).
     * Iteration order is the same as run.py's `commands.items() +
     * subcommands.items()`: top-level first (cmds list is sorted by
     * (group, name), so groups follow). Stable-min keeps the first
     * encountered tie, mirroring Python's `min(...)` semantics. */
    int best = INT_MAX;
    const char *best_name = NULL;
    const char *best_group = NULL;
    for (size_t i = 0; i < cmds->n; i++) {
        if (cmds->items[i].group) continue;  /* top-levels first pass */
        int d = lev(bad_dashed, cmds->items[i].name);
        if (d < best) {
            best = d;
            best_name = cmds->items[i].name;
            best_group = NULL;
        }
    }
    for (size_t i = 0; i < cmds->n; i++) {
        if (!cmds->items[i].group) continue;
        int d = lev(bad_dashed, cmds->items[i].name);
        if (d < best) {
            best = d;
            best_name = cmds->items[i].name;
            best_group = cmds->items[i].group;
        }
    }
    int threshold = (int)strlen(bad_dashed) / 2 + 1;
    if (best_name && best <= threshold) {
        if (best_group)
            fprintf(stderr, "Did you mean: %s %s %s?\n",
                    prog, best_group, best_name);
        else
            fprintf(stderr, "Did you mean: %s %s?\n", prog, best_name);
    }
}

/* True iff this binary is a single-command-dispatch CLI: exactly one
 * ungrouped @cli function whose name (after `_` → `-`) matches the program
 * name. In that case `<bin> bob` means "call the lone function with `bob`
 * as the first positional", NOT "look up command `bob`". Python detects
 * this in run.py:~2104; we replicate it from the same cache fields so
 * clichec can serve unknown-command output without misidentifying a
 * single-cmd-dispatch positional as a typo. */
static int is_single_cmd_dispatch(CmdList *cmds, const char *prog) {
    if (cmds->n != 1) return 0;
    if (cmds->items[0].group) return 0;
    /* Both `name` (already dasherised) and `prog` get compared with
     * `_`/`-` interchangeable so e.g. binary `csv_stats` matches
     * function `csv_stats` regardless of how either was spelled. */
    return cmd_name_eq(cmds->items[0].name, prog);
}

/* ============================================================
 *                          main
 * ============================================================ */

static int read_file(const char *path, Arena *a, char **out, size_t *len) {
    int fd = open(path, O_RDONLY);
    if (fd < 0) return -1;
    struct stat st;
    if (fstat(fd, &st) != 0) { close(fd); return -1; }
    char *buf = (char *)arena_alloc(a, (size_t)st.st_size + 1);
    size_t got = 0;
    while (got < (size_t)st.st_size) {
        ssize_t r = read(fd, buf + got, (size_t)st.st_size - got);
        if (r <= 0) { close(fd); return -1; }
        got += (size_t)r;
    }
    buf[got] = 0;
    close(fd);
    *out = buf;
    *len = got;
    return 0;
}

/* Identify global options that need Python (anything stateful). */
static int needs_python_for_globals(int uargc, char **uargv) {
    static const char *bail[] = {
        "--pdb", "--pip", "--uv", "--pyspy", "--cli", "--version",
        "--skip-gen", "--raw", "--notraceback", "--timing",
        NULL
    };
    for (int i = 0; i < uargc; i++) {
        for (int k = 0; bail[k]; k++) {
            if (strcmp(uargv[i], bail[k]) == 0) return 1;
        }
    }
    return 0;
}

/* ============================================================
 *                 argcomplete (shell completion)
 * ============================================================
 *
 * argcomplete protocol (per argcomplete/finders.py):
 *   - env _ARGCOMPLETE set → completion request, suppress normal output
 *   - env COMP_LINE / COMP_POINT carry the partial command line + cursor
 *   - env _ARGCOMPLETE_IFS is the candidate separator (default 0x0B)
 *   - output goes to env _ARGCOMPLETE_STDOUT_FILENAME if set, else fd 8
 *   - exit 0 after writing
 *
 * Tab completion fires on every keystroke; saving Python startup here
 * (~30–50 ms → ~2 ms) is the most user-visible win this binary delivers.
 *
 * Scope:
 *   - word index 1: top-level commands + groups (filtered by typed prefix)
 *   - word index 2: subcommand names (when word 1 is a group), or flag
 *     names (when word 1 is a top-level command)
 *   - word index 3+: flag names of <group> <subcmd>
 *   - flag completion: `--<param-name>` (with `--no-<param-name>` for
 *     bool=True), drawn from the function's cached parameters
 *
 * Anything we can't service (mid-command value completion, enum-typed
 * positional completion, completions inside flag values) emits no
 * candidates and exits 0 — argcomplete then falls back gracefully (the
 * shell shows no completion, or whatever the bash default is). We
 * deliberately do NOT defer to Python here: spawning Python on every
 * keystroke would defeat the entire reason for tab completion to be C.
 */

static FILE *open_completion_stream(void) {
    const char *fname = getenv("_ARGCOMPLETE_STDOUT_FILENAME");
    if (fname && *fname) {
        FILE *f = fopen(fname, "w");
        if (f) return f;
    }
    /* fd 8 is the canonical bash-hook channel. */
    int fd = dup(8);
    if (fd < 0) return NULL;
    FILE *f = fdopen(fd, "w");
    if (!f) { close(fd); return NULL; }
    return f;
}

/* Tokenise comp_line[:cursor] into words; honours simple whitespace splits
 * and a trailing-space "starting a new word" marker (matches the same logic
 * argcomplete uses, minus quoted-string handling — cliche's surface
 * doesn't use those).
 *
 * Stores word starts in `word_off` (offsets into comp_line) and word
 * lengths in `word_len`. Returns word count, with `*starting_new_word`
 * set to 1 when the cursor sits in whitespace (so completion is for an
 * empty prefix at index `nwords`). */
static int tokenize_comp_line(const char *cl, int cursor,
                              int *word_off, int *word_len,
                              int max_words, int *starting_new_word) {
    int n = 0, i = 0;
    while (i < cursor) {
        while (i < cursor && (cl[i] == ' ' || cl[i] == '\t')) i++;
        if (i >= cursor) break;
        if (n >= max_words) break;
        word_off[n] = i;
        while (i < cursor && cl[i] != ' ' && cl[i] != '\t') i++;
        word_len[n] = i - word_off[n];
        n++;
    }
    *starting_new_word = (cursor == 0 || cl[cursor - 1] == ' ' || cl[cursor - 1] == '\t');
    return n;
}

static int starts_with_n(const char *s, const char *p, int n) {
    for (int i = 0; i < n; i++) {
        if (s[i] != p[i]) return 0;
    }
    return 1;
}

/* Emit candidates whose prefix matches `pfx[:plen]`, joined by `ifs`.
 *
 * argcomplete appends a single trailing space ONLY when there is exactly
 * one matching candidate (the "completion is final, advance to next arg"
 * cue bash readline relies on). Multiple matches stay bare, since the
 * shell will print them as a list and the user has to keep typing.
 * Source: argcomplete/finders.py — `if len(completions) == 1 and not …`.
 *
 * We replicate that exactly: pre-filter once, count matches, then write. */
static void emit_candidates(FILE *out, const char *ifs,
                            const char **cands, int n,
                            const char *pfx, int plen) {
    /* First pass: count matches */
    int matches = 0;
    for (int i = 0; i < n; i++) {
        const char *c = cands[i];
        if (plen > 0 && (int)strlen(c) < plen) continue;
        if (plen > 0 && !starts_with_n(c, pfx, plen)) continue;
        matches++;
    }
    int wrote = 0;
    for (int i = 0; i < n; i++) {
        const char *c = cands[i];
        size_t cl = strlen(c);
        if (plen > 0 && (int)cl < plen) continue;
        if (plen > 0 && !starts_with_n(c, pfx, plen)) continue;
        if (wrote) fputs(ifs, out);
        fputs(c, out);
        if (matches == 1 && (cl == 0 || c[cl - 1] != '/')) fputc(' ', out);
        wrote = 1;
    }
}

/* Build a deduped, sorted list of (group ∪ topcmd) names. argparse also
 * registers `-h` / `--help` on every parser (default `add_help=True`), and
 * argcomplete includes those in the candidate set. We append them here so
 * `<bin> <TAB>` matches Python's output: commands+groups+`-h`+`--help`. */
static void collect_top_names(CmdList *cmds, const char ***out_arr,
                              int *out_n, Arena *a) {
    /* +2 entries for the `-h` / `--help` argparse always registers. */
    const char **arr = (const char **)arena_alloc(a, sizeof(char *) * (cmds->n + 3));
    int n = 0;
    for (size_t i = 0; i < cmds->n; i++) {
        const char *nm = cmds->items[i].group ? cmds->items[i].group
                                               : cmds->items[i].name;
        int dup_found = 0;
        for (int k = 0; k < n; k++) {
            if (strcmp(arr[k], nm) == 0) { dup_found = 1; break; }
        }
        if (!dup_found) arr[n++] = nm;
    }
    arr[n++] = "-h";
    arr[n++] = "--help";
    *out_arr = arr;
    *out_n   = n;
}

/* Collect flag names for a function: short flags (`-b`), long flags
 * (`--base`), and `--no-` variants for default=True bools. argparse's
 * always-on `-h` / `--help` are appended at the end so they appear in the
 * candidate set the same way Python's argcomplete emits them. */
static void collect_flags(const jv *fn, const char ***out_arr, int *out_n,
                          Arena *a) {
    const jv *params = jv_obj_get(fn, "parameters");
    int cap = 64, n = 0;
    const char **arr = (const char **)arena_alloc(a, sizeof(char *) * cap);
    if (!params || params->kind != JV_ARR) {
        arr[n++] = "-h";
        arr[n++] = "--help";
        *out_arr = arr; *out_n = n; return;
    }
    /* Short flags follow the same algorithm as cliche/abbrev.py. */
    char **shorts = NULL;
    compute_short_flags(params, &shorts, a);
    for (size_t i = 0; i < params->u.arr.n; i++) {
        const jv *p = &params->u.arr.items[i];
        if (p->kind != JV_OBJ) continue;
        const jv *pname = jv_obj_get(p, "name");
        const jv *pdef  = jv_obj_get(p, "default");
        if (!pname || pname->kind != JV_STR || !pdef) continue;
        const jv *isa = jv_obj_get(p, "is_args");
        const jv *isk = jv_obj_get(p, "is_kwargs");
        if ((isa && isa->kind == JV_BOOL && isa->u.b) ||
            (isk && isk->kind == JV_BOOL && isk->u.b)) continue;
        const jv *ann = jv_obj_get(p, "type_annotation");
        int is_bool = (ann && ann->kind == JV_STR && strstr(ann->u.str.s, "bool")) ||
                      (pdef->kind == JV_STR &&
                       (str_ieq(pdef->u.str.s, "True") ||
                        str_ieq(pdef->u.str.s, "False")));
        int default_true = (pdef->kind == JV_STR && str_ieq(pdef->u.str.s, "True"));

        size_t l = pname->u.str.l;
        char *flag = (char *)arena_alloc(a, l + 4);
        flag[0] = '-'; flag[1] = '-';
        for (size_t k = 0; k < l; k++)
            flag[2 + k] = (pname->u.str.s[k] == '_') ? '-' : pname->u.str.s[k];
        flag[2 + l] = 0;
        if (n + 2 >= cap) {
            cap *= 2;
            const char **na = (const char **)arena_alloc(a, sizeof(char *) * cap);
            memcpy(na, arr, sizeof(char *) * n);
            arr = na;
        }
        if (is_bool && default_true) {
            char *no_flag = (char *)arena_alloc(a, l + 6);
            no_flag[0] = '-'; no_flag[1] = '-';
            no_flag[2] = 'n'; no_flag[3] = 'o'; no_flag[4] = '-';
            for (size_t k = 0; k < l; k++)
                no_flag[5 + k] = (pname->u.str.s[k] == '_') ? '-' : pname->u.str.s[k];
            no_flag[5 + l] = 0;
            arr[n++] = no_flag;
        } else {
            arr[n++] = flag;
        }
        /* Append short flag (e.g. `-b` for --base) for non-bool params.
         * For bool flags, argparse registers either `--flag` or `--no-flag`
         * with no short variant via cliche's add_argument logic, so we skip.
         * Mirrors cliche/abbrev.py + run.py:add_params_to_parser. */
        if (!is_bool && shorts && shorts[i]) {
            if (n + 1 >= cap) {
                cap *= 2;
                const char **na = (const char **)arena_alloc(a, sizeof(char *) * cap);
                memcpy(na, arr, sizeof(char *) * n);
                arr = na;
            }
            arr[n++] = shorts[i];
        }
    }
    /* Reserve room for `-h` / `--help` and append. */
    if (n + 2 >= cap) {
        const char **na = (const char **)arena_alloc(a, sizeof(char *) * (n + 4));
        memcpy(na, arr, sizeof(char *) * n);
        arr = na;
    }
    arr[n++] = "-h";
    arr[n++] = "--help";
    *out_arr = arr;
    *out_n   = n;
}

static int do_complete(CmdList *cmds, Arena *a, const jv *enums_for_complete) {
    const char *cl = getenv("COMP_LINE");
    if (!cl) cl = "";
    const char *cp = getenv("COMP_POINT");
    int cursor = cp ? atoi(cp) : (int)strlen(cl);
    if (cursor < 0) cursor = 0;
    if (cursor > (int)strlen(cl)) cursor = (int)strlen(cl);

    int word_off[64], word_len[64];
    int starting_new = 0;
    int nwords = tokenize_comp_line(cl, cursor, word_off, word_len,
                                    64, &starting_new);

    /* current_prefix lives at word index `widx` */
    int widx;
    const char *prefix;
    int plen;
    if (starting_new) {
        widx = nwords;
        prefix = "";
        plen = 0;
    } else {
        widx = nwords - 1;
        prefix = (widx >= 0) ? (cl + word_off[widx]) : "";
        plen   = (widx >= 0) ? word_len[widx] : 0;
    }

    const char *ifs = getenv("_ARGCOMPLETE_IFS");
    if (!ifs || !*ifs) ifs = "\v";
    FILE *out = open_completion_stream();
    if (!out) return DEFER;

    if (widx <= 0) {
        /* Completing the binary itself — nothing useful from us. */
        fclose(out);
        return 0;
    }

    /* Helper: extract word i as a NUL-terminated arena string. */
    char *(words[64]) = {0};
    for (int i = 0; i < nwords; i++) {
        char *w = (char *)arena_alloc(a, (size_t)word_len[i] + 1);
        memcpy(w, cl + word_off[i], (size_t)word_len[i]);
        w[word_len[i]] = 0;
        words[i] = w;
    }

    if (widx == 1) {
        const char **arr;
        int n;
        collect_top_names(cmds, &arr, &n, a);
        emit_candidates(out, ifs, arr, n, prefix, plen);
        fclose(out);
        return 0;
    }

    /* widx >= 2 — need to know what words[1] is. */
    const char *w1 = words[1] ? words[1] : "";
    /* find a top-level command match */
    const jv *top_fn = NULL;
    for (size_t i = 0; i < cmds->n; i++) {
        if (!cmds->items[i].group && strcmp(cmds->items[i].name, w1) == 0) {
            top_fn = cmds->items[i].func;
            break;
        }
    }
    /* find a group match */
    int is_group = 0;
    for (size_t i = 0; i < cmds->n; i++) {
        if (cmds->items[i].group && strcmp(cmds->items[i].group, w1) == 0) {
            is_group = 1;
            break;
        }
    }

    if (widx == 2 && is_group) {
        /* completing a subcommand name. argparse's group parser also
         * registers `-h` / `--help`, so include those for parity. */
        const char **arr = (const char **)arena_alloc(a, sizeof(char *) * (cmds->n + 3));
        int n = 0;
        for (size_t i = 0; i < cmds->n; i++) {
            if (cmds->items[i].group && strcmp(cmds->items[i].group, w1) == 0) {
                arr[n++] = cmds->items[i].name;
            }
        }
        arr[n++] = "-h";
        arr[n++] = "--help";
        emit_candidates(out, ifs, arr, n, prefix, plen);
        fclose(out);
        return 0;
    }

    /* Resolve the function we're completing inside (top-level cmd OR
     * group+subcmd) and remember where positional args start in `words`. */
    const jv *target_fn = NULL;
    int pos_start = -1;  /* word index where positional args begin */
    if (top_fn) {
        target_fn = top_fn;
        pos_start = 2;
    } else if (is_group && nwords >= 3) {
        const char *w2 = words[2] ? words[2] : "";
        for (size_t i = 0; i < cmds->n; i++) {
            if (cmds->items[i].group &&
                strcmp(cmds->items[i].group, w1) == 0 &&
                strcmp(cmds->items[i].name, w2) == 0) {
                target_fn = cmds->items[i].func;
                pos_start = 3;
                break;
            }
        }
    }

    /* Flag-name completion: `--<TAB>` or `-<TAB>`. */
    if (target_fn && plen >= 1 && prefix[0] == '-') {
        const char **arr; int n;
        collect_flags(target_fn, &arr, &n, a);
        emit_candidates(out, ifs, arr, n, prefix, plen);
        fclose(out);
        return 0;
    }

    /* Value completion: positional choices, or value-of-prev-flag choices.
     *
     * Two cases share the same machinery:
     *  - `mds <TAB>`             → first positional of mds
     *  - `mds bitmex <TAB>`      → second positional (none for mds → empty)
     *  - `mds --base <TAB>`      → value for --base flag (an enum → emit its values)
     *  - `mds --base BTC <TAB>`  → next positional, since --base BTC is consumed
     *
     * Heuristic: if the previous word is a non-bool flag, complete its
     * value. Otherwise, walk words[pos_start..widx-1] counting positionals
     * (skipping flag-pairs) and offer choices for the next positional.
     *
     * "Non-bool flag" detection: matches the param by name; if the param's
     * annotation contains "bool" OR its default is True/False, the flag
     * doesn't take a value (`store_true` / `store_false` argparse action).
     */
    if (!target_fn || pos_start < 0) {
        fclose(out);
        return 0;
    }
    const jv *params = jv_obj_get(target_fn, "parameters");
    if (!params || params->kind != JV_ARR) {
        fclose(out);
        return 0;
    }

    /* Returns 1 if param `p` is a bool flag (no value follows). */
    #define PARAM_IS_BOOL(p) ({ \
        const jv *_ann = jv_obj_get((p), "type_annotation"); \
        const jv *_def = jv_obj_get((p), "default"); \
        int _b = (_ann && _ann->kind == JV_STR && \
                  strstr(_ann->u.str.s, "bool")); \
        if (!_b && _def && _def->kind == JV_STR) \
            _b = str_ieq(_def->u.str.s, "True") || str_ieq(_def->u.str.s, "False"); \
        _b; \
    })

    /* If previous word is a non-bool flag, complete its value. */
    if (widx >= pos_start + 1) {
        const char *prev = words[widx - 1];
        if (prev && (prev[0] == '-')) {
            /* extract canonical flag name (strip leading - / --). */
            const char *fname = prev[1] == '-' ? prev + 2 : prev + 1;
            /* "--no-foo" → strip the `no-` prefix to find the underlying param. */
            if (strncmp(fname, "no-", 3) == 0) fname += 3;
            for (size_t i = 0; i < params->u.arr.n; i++) {
                const jv *p = &params->u.arr.items[i];
                if (p->kind != JV_OBJ) continue;
                const jv *pn = jv_obj_get(p, "name");
                if (!pn || pn->kind != JV_STR) continue;
                /* Compare with both '_' and '-' treated as equal. */
                if (cmd_name_eq(pn->u.str.s, fname)) {
                    if (PARAM_IS_BOOL(p)) break;  /* no value to complete */
                    const jv *ann = jv_obj_get(p, "type_annotation");
                    if (enums_for_complete && ann && ann->kind == JV_STR) {
                        const jv *evals = enum_for_annotation(enums_for_complete,
                                                              ann->u.str.s);
                        if (evals && evals->kind == JV_ARR) {
                            const char **arr = (const char **)arena_alloc(
                                a, sizeof(char *) * evals->u.arr.n);
                            int n = 0;
                            for (size_t k = 0; k < evals->u.arr.n; k++) {
                                const jv *v = &evals->u.arr.items[k];
                                if (v->kind == JV_STR) arr[n++] = v->u.str.s;
                            }
                            emit_candidates(out, ifs, arr, n, prefix, plen);
                        }
                    }
                    fclose(out);
                    return 0;
                }
            }
            /* Unknown flag — emit nothing rather than guess. */
            fclose(out);
            return 0;
        }
    }

    /* Positional completion: count consumed positionals, look up the next. */
    int consumed = 0;
    for (int i = pos_start; i < widx; i++) {
        const char *w = words[i];
        if (!w) continue;
        if (w[0] == '-') {
            /* Could be a flag with a value; need to know if it's bool. */
            const char *fname = w[1] == '-' ? w + 2 : w + 1;
            if (strncmp(fname, "no-", 3) == 0) fname += 3;
            int found_param = 0, is_bool_flag = 0;
            for (size_t k = 0; k < params->u.arr.n; k++) {
                const jv *p = &params->u.arr.items[k];
                if (p->kind != JV_OBJ) continue;
                const jv *pn = jv_obj_get(p, "name");
                if (!pn || pn->kind != JV_STR) continue;
                if (cmd_name_eq(pn->u.str.s, fname)) {
                    found_param = 1;
                    is_bool_flag = PARAM_IS_BOOL(p);
                    break;
                }
            }
            if (found_param && !is_bool_flag) {
                /* Skip the flag's value too (consumes 2 words instead of 1). */
                if (i + 1 < widx) i++;
            }
            continue;
        }
        consumed++;
    }

    /* Find the (consumed)-th positional in the param list. */
    int pos_idx = 0;
    const jv *next_pos = NULL;
    for (size_t i = 0; i < params->u.arr.n; i++) {
        const jv *p = &params->u.arr.items[i];
        if (p->kind != JV_OBJ) continue;
        const jv *isa = jv_obj_get(p, "is_args");
        const jv *isk = jv_obj_get(p, "is_kwargs");
        if ((isa && isa->kind == JV_BOOL && isa->u.b) ||
            (isk && isk->kind == JV_BOOL && isk->u.b)) continue;
        if (jv_obj_get(p, "default")) continue;  /* optional → not positional */
        const jv *pn = jv_obj_get(p, "name");
        if (!pn || pn->kind != JV_STR) continue;
        if (strcmp(pn->u.str.s, "self") == 0 ||
            strcmp(pn->u.str.s, "cls") == 0) continue;
        if (pos_idx == consumed) {
            next_pos = p;
            break;
        }
        pos_idx++;
    }
    /* argparse always offers the parser's optional flags + help alongside
     * positional choices (since the user could type either). Build the
     * combined candidate set so the parity test sees the same shape. */
    const char **flag_arr; int flag_n;
    collect_flags(target_fn, &flag_arr, &flag_n, a);

    int total_cap = flag_n;
    const jv *enum_vals = NULL;
    if (next_pos) {
        const jv *ann = jv_obj_get(next_pos, "type_annotation");
        if (enums_for_complete && ann && ann->kind == JV_STR) {
            const jv *evals = enum_for_annotation(enums_for_complete,
                                                  ann->u.str.s);
            if (evals && evals->kind == JV_ARR) {
                enum_vals = evals;
                total_cap += (int)evals->u.arr.n;
            }
        }
    }
    const char **all = (const char **)arena_alloc(a, sizeof(char *) * (size_t)(total_cap + 1));
    int total_n = 0;
    for (int i = 0; i < flag_n; i++) all[total_n++] = flag_arr[i];
    if (enum_vals) {
        for (size_t k = 0; k < enum_vals->u.arr.n; k++) {
            const jv *v = &enum_vals->u.arr.items[k];
            if (v->kind == JV_STR) all[total_n++] = v->u.str.s;
        }
    }
    emit_candidates(out, ifs, all, total_n, prefix, plen);
    fclose(out);
    return 0;
    #undef PARAM_IS_BOOL
}

int main(int argc, char **argv) {
    if (argc < 3) return DEFER;
    detect_colors();
    const char *cache_path = argv[1];
    const char *pkg_name   = argv[2];
    int   uargc = argc - 3;
    char **uargv = argv + 3;

    /* prog_name resolution order:
     *   1. $CLICHEC_PROG  — set by the shell wrapper from `$0`, since POSIX
     *      sh can't rewrite argv[0] across exec (no `exec -a` portable form).
     *   2. basename of argv[0] — works when invoked directly with the binary
     *      name as the script (or via bash `exec -a name`).
     *   3. fall back to pkg_name when called as the literal `clichec*` binary. */
    const char *prog = getenv("CLICHEC_PROG");
    if (!prog || !*prog) {
        const char *slash = strrchr(argv[0], '/');
        const char *base = slash ? slash + 1 : argv[0];
        if (strncmp(base, "clichec", 7) == 0) prog = pkg_name;
        else prog = base;
    }

    int is_complete = (getenv("_ARGCOMPLETE") != NULL);
    if (!is_complete && needs_python_for_globals(uargc, uargv)) return DEFER;

    Arena a = {0};
    char *src = NULL;
    size_t slen = 0;
    if (read_file(cache_path, &a, &src, &slen) != 0) {
        arena_free(&a);
        return DEFER;
    }
    JP p = { .src = src, .i = 0, .len = slen, .a = &a, .err = 0 };
    jv root;
    if (parse_value(&p, &root) || p.err) {
        arena_free(&a);
        return DEFER;
    }
    if (root.kind != JV_OBJ) { arena_free(&a); return DEFER; }

    /* schema gate */
    const jv *ver = jv_obj_get(&root, "version");
    if (!ver || ver->kind != JV_STR ||
        strcmp(ver->u.str.s, EXPECTED_CACHE_VERSION) != 0) {
        arena_free(&a);
        return DEFER;
    }

    /* freshness — figure out pkg_dir from any cached function file_path
     * by stripping the relative module path. We use the first file's
     * file_path as the source-of-truth and strip its rel_path. */
    const char *pkg_dir = NULL;
    char pkg_dir_buf[4096];
    const jv *files = jv_obj_get(&root, "files");
    if (files && files->kind == JV_OBJ) {
        for (size_t i = 0; i < files->u.obj.n && !pkg_dir; i++) {
            const char *rel = files->u.obj.keys[i];
            size_t      rl  = files->u.obj.klens[i];
            const jv *finfo = &files->u.obj.vals[i];
            const jv *fns = jv_obj_get(finfo, "functions");
            if (!fns || fns->kind != JV_ARR || !fns->u.arr.n) continue;
            const jv *fp = jv_obj_get(&fns->u.arr.items[0], "file_path");
            if (!fp || fp->kind != JV_STR) continue;
            if (fp->u.str.l <= rl + 1) continue;
            size_t base = fp->u.str.l - rl;
            /* file_path ends with rel_path; strip "/<rel>" */
            if (memcmp(fp->u.str.s + base, rel, rl) != 0) continue;
            if (base && fp->u.str.s[base - 1] == '/') base--;
            if (base >= sizeof(pkg_dir_buf)) continue;
            memcpy(pkg_dir_buf, fp->u.str.s, base);
            pkg_dir_buf[base] = 0;
            pkg_dir = pkg_dir_buf;
        }
    }

    if (getenv("CLICHEC_DEBUG"))
        fprintf(stderr, "clichec: pkg_dir=%s\n", pkg_dir ? pkg_dir : "(null)");
    if (!cache_is_fresh(&root, pkg_dir)) {
        if (getenv("CLICHEC_DEBUG"))
            fprintf(stderr, "clichec: stale cache (deferring)\n");
        arena_free(&a);
        return DEFER;
    }

    CmdList cmds = {0};
    build_index(&root, &cmds, &a);

    if (is_complete) {
        int rc = do_complete(&cmds, &a, jv_obj_get(&root, "enums"));
        free(cmds.items);
        arena_free(&a);
        return rc;
    }

    /* dispatch */
    int rc = DEFER;
    if (uargc == 0) {
        render_top_help(prog, &cmds);
        rc = 0;
    } else if (uargc == 1 &&
               (strcmp(uargv[0], "-h") == 0 || strcmp(uargv[0], "--help") == 0)) {
        render_top_help(prog, &cmds);
        rc = 0;
    } else if (uargc == 1 && strcmp(uargv[0], "--llm-help") == 0) {
        /* Top-level --llm-help embeds an env snapshot (Python version,
         * interpreter, autocomplete state, package version from pyproject)
         * that's awkward to faithfully reproduce in C. Defer to Python so
         * the LLM-facing surface stays canonical and we never drift from
         * what `_print_llm_output_lines` emits. Per-command --llm-help
         * (next branch) is simpler and stays C-served. */
        rc = DEFER;
    } else if (uargc >= 2 && strcmp(uargv[uargc - 1], "--llm-help") == 0) {
        /* <cmd> --llm-help  OR  <group> <cmd> --llm-help */
        if (uargc == 2) {
            const char *cmd = uargv[0];
            for (size_t i = 0; i < cmds.n; i++) {
                if (!cmds.items[i].group &&
                    cmd_name_eq(cmds.items[i].name, cmd)) {
                    rc = render_command_llm(&root, prog,
                                            cmds.items[i].func, NULL, cmd);
                    goto done;
                }
            }
            /* maybe it's a group name → defer to Python which prints group help */
            for (size_t i = 0; i < cmds.n; i++) {
                if (cmds.items[i].group &&
                    cmd_name_eq(cmds.items[i].group, cmd)) {
                    rc = DEFER;
                    goto done;
                }
            }
            /* `<typo> --llm-help` on a single-cmd-dispatch CLI might be the
             * legit positional value of the lone function (`scd_solo bob
             * --llm-help`). Python decides; we defer. Otherwise it's a real
             * typo and we serve the canonical error from cache. */
            if (is_single_cmd_dispatch(&cmds, prog)) {
                rc = DEFER;
            } else {
                unknown_command(prog, cmd, &cmds, &a);
                rc = 1;
            }
        } else if (uargc == 3) {
            const char *grp = uargv[0];
            const char *cmd = uargv[1];
            for (size_t i = 0; i < cmds.n; i++) {
                if (cmds.items[i].group &&
                    cmd_name_eq(cmds.items[i].group, grp) &&
                    cmd_name_eq(cmds.items[i].name, cmd)) {
                    rc = render_command_llm(&root, prog,
                                            cmds.items[i].func, grp, cmd);
                    goto done;
                }
            }
            rc = DEFER;
        } else {
            rc = DEFER;
        }
    } else if (uargc == 2 && uargv[0][0] != '-' &&
               (strcmp(uargv[1], "-h") == 0 || strcmp(uargv[1], "--help") == 0)) {
        /* `<cmd> --help`. We render an argparse-shaped help from the cache.
         *
         * Byte-exact parity with run.py's argparse output isn't a goal —
         * argparse's HelpFormatter does line-wrapping, metavar quoting, and
         * choice display we don't (and shouldn't) reimplement. The parity
         * test asserts *content* parity instead: rc=0, all param names
         * appear, defaults appear, choices appear. Drift in the rendering
         * style is acceptable; missing information is not. */
        const char *cmd = uargv[0];
        const jv *fn = NULL;
        for (size_t i = 0; i < cmds.n; i++) {
            if (!cmds.items[i].group && cmd_name_eq(cmds.items[i].name, cmd)) {
                fn = cmds.items[i].func;
                break;
            }
        }
        if (fn) {
            rc = render_command_help(&root, prog, fn, NULL, cmd, &a);
        } else {
            /* `<group> --help` — group help generation lives in run.py. */
            rc = DEFER;
        }
    } else if (uargc == 3 && uargv[0][0] != '-' && uargv[1][0] != '-' &&
               (strcmp(uargv[2], "-h") == 0 || strcmp(uargv[2], "--help") == 0)) {
        /* `<group> <cmd> --help` — same content-parity contract as above. */
        const char *grp = uargv[0];
        const char *cmd = uargv[1];
        const jv *fn = NULL;
        for (size_t i = 0; i < cmds.n; i++) {
            if (cmds.items[i].group &&
                cmd_name_eq(cmds.items[i].group, grp) &&
                cmd_name_eq(cmds.items[i].name, cmd)) {
                fn = cmds.items[i].func;
                break;
            }
        }
        if (fn) {
            rc = render_command_help(&root, prog, fn, grp, cmd, &a);
        } else {
            rc = DEFER;
        }
    } else if (uargc >= 1 && uargv[0][0] != '-') {
        /* unknown top-level command / typo path */
        const char *cand = uargv[0];
        int top_match = 0, group_match = 0;
        for (size_t i = 0; i < cmds.n; i++) {
            if (!cmds.items[i].group && cmd_name_eq(cmds.items[i].name, cand)) {
                top_match = 1;
                break;
            }
            if (cmds.items[i].group && cmd_name_eq(cmds.items[i].group, cand)) {
                group_match = 1;
            }
        }
        if (!top_match && !group_match) {
            /* Three possible interpretations of `<bin> <unknown>`:
             *   1. Real typo on a multi-cmd CLI → emit "Unknown command".
             *   2. Single-cmd-dispatch positional value (`scd_solo bob`
             *      where `bob` is the value of the only positional) →
             *      defer so Python's `len(commands)==1 and not subcommands`
             *      shortcut at run.py:~2104 dispatches it correctly.
             *   3. Unknown but ambiguous (multi-cmd, single-arg-fn-named-
             *      `<bin>`-not-present) → fall through to Python so any
             *      future dispatch logic stays canonical.
             * The cache tells us which by counting commands and checking
             * the lone command's name against prog. */
            if (is_single_cmd_dispatch(&cmds, prog)) {
                rc = DEFER;
            } else {
                unknown_command(prog, cand, &cmds, &a);
                rc = 1;
            }
        } else {
            rc = DEFER;
        }
    } else {
        rc = DEFER;
    }

done:
    free(cmds.items);
    arena_free(&a);
    return rc;
}

