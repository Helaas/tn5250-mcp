// Helper process for tn5250-mcp: SQL and IFS access to IBM i through the
// IBM Toolbox for Java (jt400 / JTOpen), which the user supplies.
//
// Run as a single-file program (Java 11+), no build step:
//     java -cp jt400.jar T5250Helper.java
//
// Protocol: one JSON object per line on stdin, one JSON reply per line on
// stdout: {"id": 1, "op": "query", ...} -> {"id": 1, "ok": true, "result": ...}
// or {"id": 1, "ok": false, "error": "..."}. The first line written is
// {"ready": true, "jt400": "<version>"}. Passwords arrive in "connect"
// requests and are never written anywhere.

import com.ibm.as400.access.AS400;
import com.ibm.as400.access.AS400JDBCDriver;
import com.ibm.as400.access.AS400Message;
import com.ibm.as400.access.AS400Exception;
import com.ibm.as400.access.CharConverter;
import com.ibm.as400.access.IFSFile;
import com.ibm.as400.access.IFSFileInputStream;
import com.ibm.as400.access.IFSFileOutputStream;
import com.ibm.as400.access.IFSFileReader;
import com.ibm.as400.access.SecureAS400;

import java.io.BufferedReader;
import java.io.FileDescriptor;
import java.io.FileOutputStream;
import java.io.InputStreamReader;
import java.io.PrintStream;
import java.math.BigDecimal;
import java.nio.charset.StandardCharsets;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.ResultSetMetaData;
import java.sql.SQLException;
import java.sql.Statement;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Base64;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Properties;

public class T5250Helper {

    static final class Conn {
        AS400 system;
        Connection readOnly;
        Connection readWrite;
    }

    static final Map<String, Conn> CONNS = new LinkedHashMap<>();

    public static void main(String[] args) throws Exception {
        PrintStream out = new PrintStream(new FileOutputStream(FileDescriptor.out), true, "UTF-8");
        System.setOut(System.err);           // anything jt400 prints stays off the protocol
        BufferedReader in = new BufferedReader(new InputStreamReader(System.in, StandardCharsets.UTF_8));
        Map<String, Object> hello = new LinkedHashMap<>();
        hello.put("ready", true);
        hello.put("jt400", jt400Version());
        out.println(Json.write(hello));
        String line;
        while ((line = in.readLine()) != null) {
            if (line.trim().isEmpty()) continue;
            Map<String, Object> resp = new LinkedHashMap<>();
            try {
                @SuppressWarnings("unchecked")
                Map<String, Object> req = (Map<String, Object>) Json.parse(line);
                resp.put("id", req.get("id"));
                resp.put("result", handle(req));
                resp.put("ok", true);
            } catch (Throwable e) {
                resp.put("ok", false);
                resp.put("error", describe(e));
            }
            out.println(Json.write(resp));
        }
        for (Conn c : CONNS.values()) close(c);
    }

    static String jt400Version() {
        try {
            Class<?> c = Class.forName("com.ibm.as400.access.Copyright");
            return String.valueOf(c.getField("version").get(null)).trim();
        } catch (Throwable e) {
            String v = AS400.class.getPackage().getImplementationVersion();
            return v == null ? "unknown" : v;
        }
    }

    // -- requests -------------------------------------------------------------
    static Object handle(Map<String, Object> r) throws Exception {
        String op = str(r, "op");
        switch (op) {
            case "ping": return "pong";
            case "connect": return connect(r);
            case "close": {
                Conn c = CONNS.remove(str(r, "name"));
                if (c != null) close(c);
                return true;
            }
            case "query": return sql(r, false);
            case "execute": return sql(r, true);
            case "ifs_list": return ifsList(r);
            case "ifs_read": return ifsRead(r);
            case "ifs_write": return ifsWrite(r);
            default: throw new IllegalArgumentException("unknown op " + op);
        }
    }

    static Object connect(Map<String, Object> r) throws Exception {
        String name = str(r, "name");
        Conn old = CONNS.remove(name);
        if (old != null) close(old);
        char[] pw = str(r, "password").toCharArray();
        boolean tls = Boolean.TRUE.equals(r.get("tls"));
        AS400 sys = tls ? new SecureAS400(str(r, "host"), str(r, "user"), pw)
                        : new AS400(str(r, "host"), str(r, "user"), pw);
        Arrays.fill(pw, '\0');
        sys.setGuiAvailable(false);
        sys.connectService(AS400.SIGNON);    // fails here on a wrong user or password
        Conn c = new Conn();
        c.system = sys;
        CONNS.put(name, c);
        Map<String, Object> res = new LinkedHashMap<>();
        res.put("user", sys.getUserId());
        res.put("host", sys.getSystemName());
        res.put("release", "V" + sys.getVersion() + "R" + sys.getRelease() + "M" + sys.getModification());
        res.put("tls", tls);
        return res;
    }

    static Conn conn(Map<String, Object> r) {
        Conn c = CONNS.get(str(r, "name"));
        if (c == null) throw new IllegalStateException("not connected (" + str(r, "name") + ")");
        return c;
    }

    static Connection jdbc(Conn c, boolean write) throws SQLException {
        Connection have = write ? c.readWrite : c.readOnly;
        if (have != null && !have.isClosed()) return have;
        Properties p = new Properties();
        // read only: the database server itself refuses anything but queries
        p.setProperty("access", write ? "all" : "read only");
        p.setProperty("naming", "system");   // MYLIB/TABLE, and the library list
        p.setProperty("errors", "full");
        p.setProperty("prompt", "false");
        p.setProperty("date format", "iso");
        p.setProperty("time format", "iso");
        Connection made = new AS400JDBCDriver().connect(c.system, p, null);
        made.setAutoCommit(true);
        if (write) c.readWrite = made; else c.readOnly = made;
        return made;
    }

    static Object sql(Map<String, Object> r, boolean write) throws Exception {
        Conn c = conn(r);
        int maxRows = num(r, "max_rows", 100);
        int timeout = num(r, "timeout", 60);
        try (PreparedStatement ps = jdbc(c, write).prepareStatement(str(r, "sql"))) {
            ps.setMaxRows(maxRows + 1);
            ps.setQueryTimeout(timeout);
            Object params = r.get("params");
            if (params instanceof List) {
                List<?> l = (List<?>) params;
                for (int i = 0; i < l.size(); i++) {
                    Object v = l.get(i);
                    if (v instanceof Double) v = BigDecimal.valueOf((Double) v);
                    ps.setObject(i + 1, v);
                }
            }
            Map<String, Object> res = new LinkedHashMap<>();
            if (ps.execute()) {
                try (ResultSet rs = ps.getResultSet()) {
                    ResultSetMetaData md = rs.getMetaData();
                    List<Object> cols = new ArrayList<>(), types = new ArrayList<>();
                    for (int i = 1; i <= md.getColumnCount(); i++) {
                        cols.add(md.getColumnLabel(i));
                        types.add(md.getColumnTypeName(i));
                    }
                    List<Object> rows = new ArrayList<>();
                    boolean truncated = false;
                    while (rs.next()) {
                        if (rows.size() >= maxRows) { truncated = true; break; }
                        List<Object> row = new ArrayList<>();
                        for (int i = 1; i <= cols.size(); i++) {
                            String v = rs.getString(i);
                            row.add(v == null ? null : rtrim(v));
                        }
                        rows.add(row);
                    }
                    res.put("columns", cols);
                    res.put("types", types);
                    res.put("rows", rows);
                    res.put("truncated", truncated);
                }
            } else {
                res.put("update_count", ps.getUpdateCount());
            }
            return res;
        }
    }

    static IFSFile ifs(Map<String, Object> r) {
        return new IFSFile(conn(r).system, str(r, "path"));
    }

    static Object ifsList(Map<String, Object> r) throws Exception {
        IFSFile f = ifs(r);
        if (!f.exists()) throw new IllegalArgumentException("no such file or directory: " + f.getPath());
        List<Object> out = new ArrayList<>();
        IFSFile[] entries = f.isDirectory() ? f.listFiles() : new IFSFile[]{f};
        if (entries == null) entries = new IFSFile[0];
        Arrays.sort(entries, (a, b) -> a.getName().compareToIgnoreCase(b.getName()));
        int max = num(r, "max_entries", 500);
        for (IFSFile e : entries) {
            if (out.size() >= max) break;
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("name", e.getName());
            m.put("dir", e.isDirectory());
            m.put("size", e.isDirectory() ? 0L : e.length());
            m.put("modified", e.lastModified());
            out.add(m);
        }
        Map<String, Object> res = new LinkedHashMap<>();
        res.put("path", f.getPath());
        res.put("dir", f.isDirectory());
        res.put("entries", out);
        res.put("truncated", entries.length > out.size());
        return res;
    }

    static Object ifsRead(Map<String, Object> r) throws Exception {
        IFSFile f = ifs(r);
        if (!f.exists()) throw new IllegalArgumentException("no such file: " + f.getPath());
        if (f.isDirectory()) throw new IllegalArgumentException(f.getPath() + " is a directory");
        int max = num(r, "max_bytes", 100000);
        Map<String, Object> res = new LinkedHashMap<>();
        long size = f.length();
        res.put("size", size);
        res.put("ccsid", f.getCCSID());
        if (Boolean.TRUE.equals(r.get("binary"))) {
            byte[] buf = new byte[(int) Math.min(size, max)];
            int got = 0;
            try (IFSFileInputStream is = new IFSFileInputStream(f)) {
                while (got < buf.length) {
                    int n = is.read(buf, got, buf.length - got);
                    if (n < 0) break;
                    got += n;
                }
            }
            res.put("base64", Base64.getEncoder().encodeToString(Arrays.copyOf(buf, got)));
            res.put("truncated", size > got);
        } else {
            StringBuilder sb = new StringBuilder();
            char[] cb = new char[8192];
            boolean truncated = false;
            try (IFSFileReader rd = new IFSFileReader(f)) {   // converts from the file's CCSID
                int n;
                while ((n = rd.read(cb)) > 0) {
                    if (sb.length() + n > max) {
                        sb.append(cb, 0, max - sb.length());
                        truncated = true;
                        break;
                    }
                    sb.append(cb, 0, n);
                }
            }
            res.put("text", sb.toString());
            res.put("truncated", truncated);
        }
        return res;
    }

    static Object ifsWrite(Map<String, Object> r) throws Exception {
        IFSFile f = ifs(r);
        boolean append = Boolean.TRUE.equals(r.get("append"));
        boolean overwrite = Boolean.TRUE.equals(r.get("overwrite"));
        if (f.exists() && f.isDirectory()) throw new IllegalArgumentException(f.getPath() + " is a directory");
        if (f.exists() && !append && !overwrite)
            throw new IllegalArgumentException(f.getPath() + " exists; pass overwrite or append");
        boolean binary = r.get("base64") != null;
        if (!f.exists()) {
            // create it first and tag it: text is then written in that CCSID
            if (!f.createNewFile())
                throw new IllegalArgumentException("could not create " + f.getPath());
            f.setCCSID(binary ? 65535 : num(r, "ccsid", 1208));
        }
        byte[] data;
        if (binary) {
            data = Base64.getDecoder().decode(str(r, "base64"));
        } else {
            int ccsid = f.getCCSID();               // an existing file keeps its CCSID
            if (ccsid == 1208 || ccsid == 65535 || ccsid <= 0)
                data = str(r, "text").getBytes(StandardCharsets.UTF_8);
            else
                data = new CharConverter(ccsid, conn(r).system).stringToByteArray(str(r, "text"));
        }
        try (IFSFileOutputStream os = new IFSFileOutputStream(f, IFSFileOutputStream.SHARE_ALL, append)) {
            os.write(data);
        }
        Map<String, Object> res = new LinkedHashMap<>();
        res.put("path", f.getPath());
        res.put("size", f.length());
        res.put("ccsid", f.getCCSID());
        return res;
    }

    // -- helpers ----------------------------------------------------------------
    static void close(Conn c) {
        for (Connection x : new Connection[]{c.readOnly, c.readWrite}) {
            try { if (x != null) x.close(); } catch (Exception ignored) { }
        }
        try { c.system.disconnectAllServices(); } catch (Exception ignored) { }
    }

    static String describe(Throwable e) {
        StringBuilder sb = new StringBuilder();
        String m = e.getMessage();
        sb.append(m == null || m.isEmpty() ? e.getClass().getSimpleName() : m.trim());
        if (e instanceof SQLException && ((SQLException) e).getSQLState() != null)
            sb.append(" (SQLSTATE ").append(((SQLException) e).getSQLState()).append(")");
        if (e instanceof AS400Exception) {
            for (AS400Message msg : ((AS400Exception) e).getAS400MessageList())
                sb.append("\n").append(msg.getID()).append(": ").append(msg.getText());
        }
        return sb.toString();
    }

    static String rtrim(String s) {
        int n = s.length();
        while (n > 0 && s.charAt(n - 1) == ' ') n--;
        return s.substring(0, n);
    }

    static String str(Map<String, Object> r, String k) {
        Object v = r.get(k);
        if (v == null) throw new IllegalArgumentException("missing " + k);
        return String.valueOf(v);
    }

    static int num(Map<String, Object> r, String k, int dflt) {
        Object v = r.get(k);
        return v instanceof Number ? ((Number) v).intValue() : dflt;
    }

    // -- a small JSON reader/writer (the JDK has none) ---------------------------
    static final class Json {
        private final String s;
        private int i;

        private Json(String s) { this.s = s; }

        static Object parse(String text) {
            Json j = new Json(text);
            Object v = j.value();
            j.ws();
            if (j.i != j.s.length()) throw new IllegalArgumentException("trailing characters in JSON");
            return v;
        }

        private void ws() {
            while (i < s.length() && Character.isWhitespace(s.charAt(i))) i++;
        }

        private Object value() {
            ws();
            if (i >= s.length()) throw new IllegalArgumentException("unexpected end of JSON");
            char c = s.charAt(i);
            if (c == '{') {
                i++;
                Map<String, Object> m = new LinkedHashMap<>();
                ws();
                if (s.charAt(i) == '}') { i++; return m; }
                while (true) {
                    ws();
                    String k = string();
                    ws();
                    expect(':');
                    m.put(k, value());
                    ws();
                    if (s.charAt(i) == ',') { i++; continue; }
                    expect('}');
                    return m;
                }
            }
            if (c == '[') {
                i++;
                List<Object> l = new ArrayList<>();
                ws();
                if (s.charAt(i) == ']') { i++; return l; }
                while (true) {
                    l.add(value());
                    ws();
                    if (s.charAt(i) == ',') { i++; continue; }
                    expect(']');
                    return l;
                }
            }
            if (c == '"') return string();
            if (s.startsWith("true", i)) { i += 4; return Boolean.TRUE; }
            if (s.startsWith("false", i)) { i += 5; return Boolean.FALSE; }
            if (s.startsWith("null", i)) { i += 4; return null; }
            int st = i;
            while (i < s.length() && "+-0123456789.eE".indexOf(s.charAt(i)) >= 0) i++;
            String n = s.substring(st, i);
            if (n.isEmpty()) throw new IllegalArgumentException("bad JSON at " + st);
            if (n.contains(".") || n.contains("e") || n.contains("E")) return Double.valueOf(n);
            return Long.valueOf(n);
        }

        private void expect(char c) {
            if (i >= s.length() || s.charAt(i) != c)
                throw new IllegalArgumentException("expected '" + c + "' in JSON at " + i);
            i++;
        }

        private String string() {
            expect('"');
            StringBuilder sb = new StringBuilder();
            while (true) {
                char c = s.charAt(i++);
                if (c == '"') return sb.toString();
                if (c != '\\') { sb.append(c); continue; }
                char e = s.charAt(i++);
                switch (e) {
                    case 'n': sb.append('\n'); break;
                    case 't': sb.append('\t'); break;
                    case 'r': sb.append('\r'); break;
                    case 'b': sb.append('\b'); break;
                    case 'f': sb.append('\f'); break;
                    case 'u': sb.append((char) Integer.parseInt(s.substring(i, i + 4), 16)); i += 4; break;
                    default: sb.append(e);
                }
            }
        }

        static String write(Object o) {
            StringBuilder sb = new StringBuilder();
            write(sb, o);
            return sb.toString();
        }

        @SuppressWarnings("unchecked")
        private static void write(StringBuilder sb, Object o) {
            if (o == null) { sb.append("null"); return; }
            if (o instanceof Boolean || o instanceof Integer || o instanceof Long) { sb.append(o); return; }
            if (o instanceof Number) { sb.append(new BigDecimal(o.toString()).toPlainString()); return; }
            if (o instanceof Map) {
                sb.append('{');
                boolean first = true;
                for (Map.Entry<String, Object> e : ((Map<String, Object>) o).entrySet()) {
                    if (!first) sb.append(',');
                    first = false;
                    quote(sb, e.getKey());
                    sb.append(':');
                    write(sb, e.getValue());
                }
                sb.append('}');
                return;
            }
            if (o instanceof List) {
                sb.append('[');
                boolean first = true;
                for (Object v : (List<Object>) o) {
                    if (!first) sb.append(',');
                    first = false;
                    write(sb, v);
                }
                sb.append(']');
                return;
            }
            quote(sb, String.valueOf(o));
        }

        private static void quote(StringBuilder sb, String s) {
            sb.append('"');
            for (int k = 0; k < s.length(); k++) {
                char c = s.charAt(k);
                switch (c) {
                    case '"': sb.append("\\\""); break;
                    case '\\': sb.append("\\\\"); break;
                    case '\n': sb.append("\\n"); break;
                    case '\r': sb.append("\\r"); break;
                    case '\t': sb.append("\\t"); break;
                    default:
                        if (c < 0x20 || c == 0x2028 || c == 0x2029) sb.append(String.format("\\u%04x", (int) c));
                        else sb.append(c);
                }
            }
            sb.append('"');
        }
    }
}
