/*
 * GenApp off-mainframe test runtime.
 *
 * Small stand-ins for the CICS and Db2 services used by GenApp, so the
 * COBOL programs can run unchanged (after test/tools/precompile.py) on
 * Linux with GnuCOBOL and PostgreSQL.
 *
 *   CICS  : program LINK (done in COBOL), VSAM files (one file per record
 *           under $GENAPP_DATA/vsam), TS/TD queues (appended to
 *           $GENAPP_DATA/queues), ABEND, SYNCPOINT.
 *   Db2   : EXEC SQL statements run through libpq. SQLCODE / SQLSTATE /
 *           SQLERRD are filled the way Db2 would fill them for the cases
 *           GenApp relies on (0, +100, -530, -803, -811, -305 ...).
 *
 * Every routine is called from COBOL with BY REFERENCE arguments only.
 */
#include <ctype.h>
#include <errno.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <sys/stat.h>
#include <unistd.h>

#include <libpq-fe.h>

/* ------------------------------------------------------------------ */
/* Helpers                                                            */
/* ------------------------------------------------------------------ */

static void die(const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    fputs("GENAPP-RUNTIME: ", stderr);
    vfprintf(stderr, fmt, ap);
    fputc('\n', stderr);
    va_end(ap);
    exit(12);
}

static void trace(const char *fmt, ...)
{
    const char *path = getenv("GENAPP_TRACE");
    if (!path || !*path)
        return;
    FILE *f = fopen(path, "a");
    if (!f)
        return;
    va_list ap;
    va_start(ap, fmt);
    vfprintf(f, fmt, ap);
    va_end(ap);
    fputc('\n', f);
    fclose(f);
}

/* Copy a blank-padded COBOL name into a C string (trailing blanks removed). */
static void cobname(char *dst, const char *src, int len)
{
    int n = len;
    while (n > 0 && (src[n - 1] == ' ' || src[n - 1] == '\0'))
        n--;
    memcpy(dst, src, n);
    dst[n] = '\0';
}

static const char *data_dir(void)
{
    const char *d = getenv("GENAPP_DATA");
    return (d && *d) ? d : "/tmp/genapp-data";
}

static void mkdirs(const char *path)
{
    char tmp[1024];
    snprintf(tmp, sizeof tmp, "%s", path);
    for (char *p = tmp + 1; *p; p++) {
        if (*p == '/') {
            *p = '\0';
            mkdir(tmp, 0777);
            *p = '/';
        }
    }
    mkdir(tmp, 0777);
}

/* ------------------------------------------------------------------ */
/* Db2 emulation on PostgreSQL                                        */
/* ------------------------------------------------------------------ */

/* Host variable type codes (must match test/tools/precompile.py). */
enum {
    HV_CHAR = 1,      /* PIC X(n)                                       */
    HV_BINARY = 2,    /* PIC S9(n) COMP / BINARY (big-endian)           */
    HV_NATIVE = 3,    /* PIC S9(n) COMP-5 (native byte order)           */
    HV_DISPLAY = 4,   /* PIC 9(n) / S9(n) zoned decimal                 */
    HV_PACKED = 5,    /* PIC S9(n) COMP-3                               */
    HV_VARCHAR = 6,   /* 49-level length (COMP) + text group            */
    HV_VARCHAR5 = 7,  /* 49-level length (COMP-5) + text group          */
};

typedef struct {
    int type;
    int len;     /* byte length of the COBOL item                   */
    int scale;   /* digits after the implied decimal point           */
    int sign;    /* 1 if the PICTURE has an S                        */
    unsigned char *data;
    int ind_type;
    int ind_len;
    unsigned char *ind;
} hostvar;

#define MAX_HV 128
static hostvar hv_in[MAX_HV], hv_out[MAX_HV];
static int n_in, n_out;
static PGconn *conn;

/* SQLCA layout (see test/runtime/SQLCA.cpy). */
#define SQLCA_SQLCODE 12
#define SQLCA_SQLERRML 16
#define SQLCA_SQLERRMC 18
#define SQLCA_SQLERRP 88
#define SQLCA_SQLERRD 96
#define SQLCA_SQLWARN 120
#define SQLCA_SQLSTATE 131

static int64_t get_be(const unsigned char *p, int len)
{
    int64_t v = (p[0] & 0x80) ? -1 : 0;
    for (int i = 0; i < len; i++)
        v = (v << 8) | p[i];
    return v;
}

static void put_be(unsigned char *p, int len, int64_t v)
{
    for (int i = len - 1; i >= 0; i--) {
        p[i] = (unsigned char)(v & 0xff);
        v >>= 8;
    }
}

static int64_t get_native(const unsigned char *p, int len)
{
    switch (len) {
    case 1: return *(const int8_t *)p;
    case 2: { int16_t v; memcpy(&v, p, 2); return v; }
    case 4: { int32_t v; memcpy(&v, p, 4); return v; }
    case 8: { int64_t v; memcpy(&v, p, 8); return v; }
    }
    die("unsupported COMP-5 length %d", len);
    return 0;
}

static void put_native(unsigned char *p, int len, int64_t v)
{
    switch (len) {
    case 1: { int8_t x = (int8_t)v; memcpy(p, &x, 1); return; }
    case 2: { int16_t x = (int16_t)v; memcpy(p, &x, 2); return; }
    case 4: { int32_t x = (int32_t)v; memcpy(p, &x, 4); return; }
    case 8: memcpy(p, &v, 8); return;
    }
    die("unsupported COMP-5 length %d", len);
}

/* Zoned decimal: digits '0'-'9'; the last byte may carry a sign
 * (GnuCOBOL ASCII: 'p'-'y' negative; EBCDIC style '{','A'-'I','}','J'-'R'). */
static int64_t get_display(const unsigned char *p, int len)
{
    int64_t v = 0;
    int neg = 0;
    for (int i = 0; i < len; i++) {
        int c = p[i], d = 0;
        if (c >= '0' && c <= '9') d = c - '0';
        else if (i == len - 1 && c >= 'p' && c <= 'y') { d = c - 'p'; neg = 1; }
        else if (i == len - 1 && c == '{') d = 0;
        else if (i == len - 1 && c >= 'A' && c <= 'I') d = c - 'A' + 1;
        else if (i == len - 1 && c == '}') { d = 0; neg = 1; }
        else if (i == len - 1 && c >= 'J' && c <= 'R') { d = c - 'J' + 1; neg = 1; }
        v = v * 10 + d; /* blanks and other junk count as zero */
    }
    return neg ? -v : v;
}

static void put_display(unsigned char *p, int len, int sign, int64_t v)
{
    int neg = v < 0;
    uint64_t u = neg ? (uint64_t)(-v) : (uint64_t)v;
    for (int i = len - 1; i >= 0; i--) {
        p[i] = (unsigned char)('0' + u % 10);
        u /= 10;
    }
    if (sign && neg)
        p[len - 1] = (unsigned char)('p' + (p[len - 1] - '0'));
}

static int64_t get_packed(const unsigned char *p, int len)
{
    int64_t v = 0;
    for (int i = 0; i < len; i++) {
        v = v * 10 + (p[i] >> 4);
        if (i < len - 1)
            v = v * 10 + (p[i] & 0x0f);
    }
    int s = p[len - 1] & 0x0f;
    return (s == 0x0d || s == 0x0b) ? -v : v;
}

static void put_packed(unsigned char *p, int len, int sign, int64_t v)
{
    int neg = v < 0;
    uint64_t u = neg ? (uint64_t)(-v) : (uint64_t)v;
    memset(p, 0, len);
    p[len - 1] = (unsigned char)(((u % 10) << 4) | (sign ? (neg ? 0x0d : 0x0c) : 0x0f));
    u /= 10;
    for (int i = len - 2; i >= 0; i--) {
        p[i] = (unsigned char)(u % 10);
        u /= 10;
        p[i] |= (unsigned char)((u % 10) << 4);
        u /= 10;
    }
}

static int64_t hv_get_int(const hostvar *h)
{
    switch (h->type) {
    case HV_BINARY: return get_be(h->data, h->len);
    case HV_NATIVE: return get_native(h->data, h->len);
    case HV_DISPLAY: return get_display(h->data, h->len);
    case HV_PACKED: return get_packed(h->data, h->len);
    }
    die("not a numeric host variable type %d", h->type);
    return 0;
}

static void hv_put_int(const hostvar *h, int64_t v)
{
    switch (h->type) {
    case HV_BINARY: put_be(h->data, h->len, v); return;
    case HV_NATIVE: put_native(h->data, h->len, v); return;
    case HV_DISPLAY: put_display(h->data, h->len, h->sign, v); return;
    case HV_PACKED: put_packed(h->data, h->len, h->sign, v); return;
    }
    die("not a numeric host variable type %d", h->type);
}

static int64_t ind_get(const hostvar *h)
{
    hostvar t = { .type = h->ind_type, .len = h->ind_len, .data = h->ind, .sign = 1 };
    return hv_get_int(&t);
}

static void ind_put(const hostvar *h, int64_t v)
{
    hostvar t = { .type = h->ind_type, .len = h->ind_len, .data = h->ind, .sign = 1 };
    hv_put_int(&t, v);
}

/* Db2 timestamp text: YYYY-MM-DD-HH.MM.SS.ffffff  <->  PostgreSQL ISO. */
static int is_db2_timestamp(const char *s, size_t n)
{
    if (n < 19) return 0;
    return isdigit((unsigned char)s[0]) && s[4] == '-' && s[7] == '-' && s[10] == '-' &&
           s[13] == '.' && s[16] == '.';
}

static void db2_to_pg_timestamp(char *s)
{
    s[10] = ' ';
    s[13] = ':';
    s[16] = ':';
}

static void pg_to_db2_timestamp(const char *in, char *out /* >= 27 */)
{
    /* in: YYYY-MM-DD HH:MM:SS[.f...] */
    memset(out, '0', 26);
    out[26] = '\0';
    size_t n = strlen(in);
    memcpy(out, in, n < 19 ? n : 19);
    out[10] = '-';
    out[13] = '.';
    out[16] = '.';
    out[19] = '.';
    if (n > 20 && in[19] == '.') {
        size_t frac = n - 20;
        for (size_t i = 0; i < frac && i < 6 && isdigit((unsigned char)in[20 + i]); i++)
            out[20 + i] = in[20 + i];
    }
}

static char *format_scaled(int64_t v, int scale)
{
    char *buf = malloc(64);
    if (scale > 18) scale = 18;
    if (scale <= 0) {
        snprintf(buf, 64, "%lld", (long long)v);
        return buf;
    }
    int64_t p = 1;
    for (int i = 0; i < scale; i++) p *= 10;
    int64_t a = v < 0 ? -v : v;
    snprintf(buf, 64, "%s%lld.%0*lld", v < 0 ? "-" : "", (long long)(a / p), scale,
             (long long)(a % p));
    return buf;
}

static int64_t parse_scaled(const char *s, int scale)
{
    int neg = 0;
    while (*s == ' ') s++;
    if (*s == '-') { neg = 1; s++; }
    else if (*s == '+') s++;
    int64_t v = 0;
    while (isdigit((unsigned char)*s)) v = v * 10 + (*s++ - '0');
    int got = 0;
    if (*s == '.') {
        s++;
        while (isdigit((unsigned char)*s)) {
            if (got < scale) { v = v * 10 + (*s - '0'); got++; }
            s++;
        }
    }
    for (; got < scale; got++) v *= 10;
    return neg ? -v : v;
}

/* Text value for an input host variable (malloc'd), NULL = SQL NULL. */
static char *hv_to_text(const hostvar *h)
{
    if (h->ind && ind_get(h) < 0)
        return NULL;
    if (h->type == HV_CHAR || h->type == HV_VARCHAR || h->type == HV_VARCHAR5) {
        const unsigned char *src = h->data;
        int n = h->len;
        if (h->type != HV_CHAR) {
            int maxlen = h->len - 2;
            n = (int)(h->type == HV_VARCHAR ? get_be(h->data, 2) : get_native(h->data, 2));
            if (n < 0) n = 0;
            if (n > maxlen) n = maxlen;
            src = h->data + 2;
        }
        char *s = malloc(n + 1);
        for (int i = 0; i < n; i++)
            s[i] = src[i] ? (char)src[i] : ' ';
        s[n] = '\0';
        if (is_db2_timestamp(s, n))
            db2_to_pg_timestamp(s);
        return s;
    }
    return format_scaled(hv_get_int(h), h->scale);
}

/* Store a result column into an output host variable. */
static void text_to_hv(const hostvar *h, const char *val, Oid type)
{
    if (h->type == HV_CHAR || h->type == HV_VARCHAR || h->type == HV_VARCHAR5) {
        char ts[32];
        if (type == 1114 || type == 1184) { /* timestamp, timestamptz */
            pg_to_db2_timestamp(val, ts);
            val = ts;
        }
        int n = (int)strlen(val);
        if (h->type == HV_CHAR) {
            int c = n < h->len ? n : h->len;
            memcpy(h->data, val, c);
            memset(h->data + c, ' ', h->len - c);
        } else {
            int maxlen = h->len - 2;
            int c = n < maxlen ? n : maxlen;
            if (h->type == HV_VARCHAR) put_be(h->data, 2, c);
            else put_native(h->data, 2, c);
            memcpy(h->data + 2, val, c);
        }
        return;
    }
    hv_put_int(h, parse_scaled(val, h->scale));
}

static void pg_exec_simple(const char *sql)
{
    PGresult *r = PQexec(conn, sql);
    if (PQresultStatus(r) != PGRES_COMMAND_OK && PQresultStatus(r) != PGRES_TUPLES_OK)
        die("'%s' failed: %s", sql, PQerrorMessage(conn));
    PQclear(r);
}

static void sql_connect(void)
{
    if (conn)
        return;
    const char *dsn = getenv("GENAPP_DSN");
    conn = PQconnectdb(dsn ? dsn : "");
    if (PQstatus(conn) != CONNECTION_OK)
        die("cannot connect to PostgreSQL: %s", PQerrorMessage(conn));
    PQsetClientEncoding(conn, "LATIN1");
    pg_exec_simple("SET datestyle TO ISO, YMD");
    pg_exec_simple("BEGIN");
}

static int sqlcode_for(const char *state, const char *sql)
{
    static const struct { const char *state; int code; } map[] = {
        {"23503", -530}, /* foreign key violation (insert/update) */
        {"23505", -803}, /* duplicate key */
        {"23502", -407}, /* NULL into NOT NULL column */
        {"22001", -404}, /* value too long */
        {"22003", -406}, /* numeric out of range */
        {"22007", -180}, /* bad datetime format */
        {"22008", -181}, /* datetime out of range */
        {"22P02", -420}, /* invalid character in numeric/cast */
        {"42P01", -204}, /* undefined table */
        {"42703", -206}, /* undefined column */
        {"42601", -104}, /* syntax error */
        {"34000", -501}, /* cursor not open */
        {"42P03", -502}, /* cursor already open */
        {"40001", -911}, /* deadlock / serialization */
        {"40P01", -911},
        {"55P03", -913}, /* lock timeout */
    };
    if (state && strcmp(state, "23503") == 0 && strncasecmp(sql, "DELETE", 6) == 0)
        return -532; /* delete restricted by a foreign key */
    for (size_t i = 0; state && i < sizeof map / sizeof map[0]; i++)
        if (strcmp(map[i].state, state) == 0)
            return map[i].code;
    return -999; /* no Db2 equivalent mapped; see SQLERRMC for the PG message */
}

static void sqlca_set(unsigned char *ca, int code, const char *state, const char *msg, int64_t rows)
{
    memcpy(ca, "SQLCA   ", 8);
    put_native(ca + 8, 4, 136);
    put_native(ca + SQLCA_SQLCODE, 4, code);
    int ml = msg ? (int)strlen(msg) : 0;
    if (ml > 70) ml = 70;
    put_native(ca + SQLCA_SQLERRML, 2, ml);
    memset(ca + SQLCA_SQLERRMC, ' ', 70);
    if (ml) memcpy(ca + SQLCA_SQLERRMC, msg, ml);
    memcpy(ca + SQLCA_SQLERRP, "GENAPPPG", 8);
    for (int i = 0; i < 6; i++)
        put_native(ca + SQLCA_SQLERRD + 4 * i, 4, i == 2 ? rows : 0);
    memset(ca + SQLCA_SQLWARN, ' ', 11);
    memcpy(ca + SQLCA_SQLSTATE, state, 5);
}

/*
 * CALL 'GASQLBND' USING descriptor length host-variable
 * CALL 'GASQLIND' USING descriptor length indicator   (for the last bound variable)
 *   descriptor = direction * 1000000 + type * 10000 + scale * 100 + signed
 *   direction: 0 = input to the statement, 1 = output (INTO)
 */
static void decode_desc(int desc, int *dir, int *type, int *scale, int *sign)
{
    *dir = desc / 1000000;
    *type = (desc / 10000) % 100;
    *scale = (desc / 100) % 100;
    *sign = desc % 100;
}

int GASQLBND(int *desc, int *len, unsigned char *data)
{
    int dir, type, scale, sign;
    decode_desc(*desc, &dir, &type, &scale, &sign);
    hostvar *h;
    if (dir == 0) {
        if (n_in >= MAX_HV) die("too many input host variables");
        h = &hv_in[n_in++];
    } else {
        if (n_out >= MAX_HV) die("too many output host variables");
        h = &hv_out[n_out++];
    }
    memset(h, 0, sizeof *h);
    h->type = type;
    h->len = *len;
    h->scale = scale;
    h->sign = sign;
    h->data = data;
    return 0;
}

int GASQLIND(int *desc, int *len, unsigned char *ind)
{
    int dir, type, scale, sign;
    decode_desc(*desc, &dir, &type, &scale, &sign);
    hostvar *h = (dir == 0) ? (n_in ? &hv_in[n_in - 1] : NULL) : (n_out ? &hv_out[n_out - 1] : NULL);
    if (!h) die("indicator without host variable");
    h->ind_type = type;
    h->ind_len = *len;
    h->ind = ind;
    return 0;
}

/* CALL 'GASQLEXE' USING SQLCA statement-text(NUL terminated) */
int GASQLEXE(unsigned char *sqlca, const char *sql)
{
    sql_connect();

    const char *values[MAX_HV];
    for (int i = 0; i < n_in; i++)
        values[i] = hv_to_text(&hv_in[i]);

    pg_exec_simple("SAVEPOINT genapp_stmt");
    PGresult *r = PQexecParams(conn, sql, n_in, NULL, values, NULL, NULL, 0);
    ExecStatusType st = PQresultStatus(r);

    int code = 0;
    int warn3 = 0;
    const char *state = "00000";
    const char *msg = NULL;
    int64_t rows = 0;

    if (st == PGRES_COMMAND_OK) {
        const char *ct = PQcmdTuples(r);
        rows = (ct && *ct) ? atoll(ct) : 0;
        const char *cs = PQcmdStatus(r);
        if ((strncmp(cs, "UPDATE", 6) == 0 || strncmp(cs, "DELETE", 6) == 0) && rows == 0) {
            code = 100;
            state = "02000";
        }
    } else if (st == PGRES_TUPLES_OK) {
        int nt = PQntuples(r);
        rows = nt;
        if (n_out > 0 && nt == 0) {
            code = 100;
            state = "02000";
        } else if (n_out > 0 && nt > 1 && strncasecmp(sql, "FETCH", 5) != 0) {
            code = -811;
            state = "21000";
            msg = "more than one row returned by SELECT INTO";
        } else if (n_out > 0) {
            /* Db2 assigns min(columns, host variables) and sets SQLWARN3. */
            int nf = PQnfields(r);
            int nv = n_out < nf ? n_out : nf;
            if (nf != n_out) warn3 = 1;
            for (int i = 0; i < nv; i++) {
                hostvar *h = &hv_out[i];
                if (PQgetisnull(r, 0, i)) {
                    if (h->ind) {
                        ind_put(h, -1);
                    } else if (code == 0) {
                        code = -305;
                        state = "22002";
                        msg = "NULL value without indicator variable";
                    }
                    continue;
                }
                if (h->ind)
                    ind_put(h, 0);
                text_to_hv(h, PQgetvalue(r, 0, i), PQftype(r, i));
            }
        }
    } else {
        state = PQresultErrorField(r, PG_DIAG_SQLSTATE);
        if (!state) state = "58000";
        msg = PQresultErrorField(r, PG_DIAG_MESSAGE_PRIMARY);
        code = sqlcode_for(state, sql);
    }

    char statebuf[6];
    snprintf(statebuf, sizeof statebuf, "%s", state);
    char msgbuf[71] = "";
    if (msg) snprintf(msgbuf, sizeof msgbuf, "%s", msg);
    sqlca_set(sqlca, code, statebuf, msgbuf, rows);
    if (warn3) {
        sqlca[SQLCA_SQLWARN] = 'W';
        sqlca[SQLCA_SQLWARN + 3] = 'W';
    }

    trace("SQL %d %s%s | %s%s%s", code, statebuf, warn3 ? " SQLWARN3" : "", sql,
          *msgbuf ? " | " : "", msgbuf);

    PQclear(r);
    if (code < 0)
        pg_exec_simple("ROLLBACK TO SAVEPOINT genapp_stmt");
    else
        pg_exec_simple("RELEASE SAVEPOINT genapp_stmt");

    for (int i = 0; i < n_in; i++)
        free((void *)values[i]);
    n_in = n_out = 0;
    return 0;
}

/* CALL 'GASQLEND' USING commit-flag (1 = commit, 0 = rollback) */
int GASQLEND(int *commit)
{
    if (!conn)
        return 0;
    pg_exec_simple(*commit ? "COMMIT" : "ROLLBACK");
    PQfinish(conn);
    conn = NULL;
    return 0;
}

/* ------------------------------------------------------------------ */
/* CICS services                                                      */
/* ------------------------------------------------------------------ */

enum { RESP_NORMAL = 0, RESP_NOTFND = 13, RESP_DUPREC = 14, RESP_INVREQ = 16, RESP_LENGERR = 22 };

static void abend(const char *code)
{
    fprintf(stderr, "GENAPP-ABEND %.4s\n", code);
    trace("ABEND %.4s", code);
    if (conn) {
        PQclear(PQexec(conn, "ROLLBACK"));
        PQfinish(conn);
        conn = NULL;
    }
    exit(16);
}

/* CALL 'CICSABND' USING abcode */
int CICSABND(const char *abcode)
{
    abend(abcode);
    return 0;
}

/* CALL 'CICSSYNC' USING rollback-flag */
int CICSSYNC(int *rollback)
{
    if (!conn)
        return 0;
    pg_exec_simple(*rollback ? "ROLLBACK" : "COMMIT");
    pg_exec_simple("BEGIN");
    trace("SYNCPOINT%s", *rollback ? " ROLLBACK" : "");
    return 0;
}

static void record_path(char *out, size_t sz, const char *file, const unsigned char *key, int keylen)
{
    char dir[512];
    snprintf(dir, sizeof dir, "%s/vsam/%s", data_dir(), file);
    mkdirs(dir);
    int n = snprintf(out, sz, "%s/", dir);
    for (int i = 0; i < keylen && n < (int)sz - 3; i++)
        n += snprintf(out + n, sz - n, "%02x", key[i]);
}

static char held_file[16];
static char held_path[1024];

/*
 * CALL 'CICSFILE' USING op file record length key keylen resp resp2 resp-wanted
 *   op: 'WRIT', 'READ', 'RDUP' (read for update), 'REWR', 'DELE'
 * If the caller did not code RESP(), a non-NORMAL condition abends the task
 * the way CICS would.
 */
int CICSFILE(const char *op, const char *file8, unsigned char *rec, int *len,
             const unsigned char *key, int *keylen, int *resp, int *resp2, int *resp_wanted)
{
    char file[9], path[1024];
    cobname(file, file8, 8);
    *resp = RESP_NORMAL;
    *resp2 = 0;

    if (memcmp(op, "REWR", 4) == 0) {
        if (strcmp(held_file, file) != 0) {
            *resp = RESP_INVREQ;
        } else {
            FILE *f = fopen(held_path, "wb");
            if (!f) die("cannot write %s: %s", held_path, strerror(errno));
            fwrite(rec, 1, *len, f);
            fclose(f);
            held_file[0] = '\0';
        }
    } else {
        record_path(path, sizeof path, file, key, *keylen);
        if (memcmp(op, "WRIT", 4) == 0) {
            if (access(path, F_OK) == 0) {
                *resp = RESP_DUPREC;
            } else {
                FILE *f = fopen(path, "wb");
                if (!f) die("cannot write %s: %s", path, strerror(errno));
                fwrite(rec, 1, *len, f);
                fclose(f);
            }
        } else if (memcmp(op, "READ", 4) == 0 || memcmp(op, "RDUP", 4) == 0) {
            FILE *f = fopen(path, "rb");
            if (!f) {
                *resp = RESP_NOTFND;
            } else {
                unsigned char buf[32768];
                int n = (int)fread(buf, 1, sizeof buf, f);
                fclose(f);
                int c = n < *len ? n : *len;
                memcpy(rec, buf, c);
                if (n > *len)
                    *resp = RESP_LENGERR;
                *len = n;
                if (memcmp(op, "RDUP", 4) == 0 && *resp == RESP_NORMAL) {
                    snprintf(held_file, sizeof held_file, "%s", file);
                    snprintf(held_path, sizeof held_path, "%s", path);
                }
            }
        } else if (memcmp(op, "DELE", 4) == 0) {
            if (unlink(path) != 0)
                *resp = RESP_NOTFND;
        } else {
            die("unknown file operation %.4s", op);
        }
    }
    trace("FILE %.4s %s resp=%d", op, file, *resp);

    if (*resp != RESP_NORMAL && !*resp_wanted) {
        switch (*resp) {
        case RESP_NOTFND: abend("AEIM"); break;
        case RESP_DUPREC: abend("AEIN"); break;
        case RESP_INVREQ: abend("AEIP"); break;
        case RESP_LENGERR: abend("AEIV"); break;
        default: abend("AEY0");
        }
    }
    return 0;
}

/* CALL 'CICSWRQ' USING kind('TS'/'TD') queue data length resp */
int CICSWRQ(const char *kind, const char *queue8, const unsigned char *data, int *len, int *resp)
{
    char queue[9], dir[512], path[1024];
    cobname(queue, queue8, 8);
    snprintf(dir, sizeof dir, "%s/queues", data_dir());
    mkdirs(dir);
    snprintf(path, sizeof path, "%s/%.2s-%s.log", dir, kind, queue);
    FILE *f = fopen(path, "ab");
    if (!f) die("cannot write %s: %s", path, strerror(errno));
    for (int i = 0; i < *len; i++)
        fputc(data[i] == '\n' || data[i] == '\0' ? ' ' : data[i], f);
    fputc('\n', f);
    fclose(f);
    *resp = RESP_NORMAL;
    return 0;
}

/* ------------------------------------------------------------------ */
/* Test driver hooks (used by CICSRUN.cbl)                            */
/* ------------------------------------------------------------------ */

/* CALL 'CICSINIT' USING program commarea calen */
int CICSINIT(char *program8, unsigned char *commarea, int *calen)
{
    const char *prog = getenv("GENAPP_PROGRAM");
    const char *in = getenv("GENAPP_COMMAREA_IN");
    if (!prog || !*prog) die("GENAPP_PROGRAM not set");
    memset(program8, ' ', 8);
    memcpy(program8, prog, strlen(prog) < 8 ? strlen(prog) : 8);
    memset(commarea, ' ', 32767);
    *calen = 0;
    if (in && *in) {
        FILE *f = fopen(in, "rb");
        if (!f) die("cannot read %s: %s", in, strerror(errno));
        *calen = (int)fread(commarea, 1, 32767, f);
        fclose(f);
    }
    const char *cl = getenv("GENAPP_CALEN");
    if (cl && *cl)
        *calen = atoi(cl);
    trace("RUN %s calen=%d", prog, *calen);
    return 0;
}

/* CALL 'CICSTERM' USING commarea calen  - normal task end: commit and write the commarea */
int CICSTERM(const unsigned char *commarea, int *calen)
{
    int one = 1;
    GASQLEND(&one);
    const char *out = getenv("GENAPP_COMMAREA_OUT");
    if (out && *out) {
        FILE *f = fopen(out, "wb");
        if (!f) die("cannot write %s: %s", out, strerror(errno));
        fwrite(commarea, 1, *calen, f);
        fclose(f);
    }
    return 0;
}
