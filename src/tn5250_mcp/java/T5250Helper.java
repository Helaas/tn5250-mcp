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
import com.ibm.as400.access.AS400Bin2;
import com.ibm.as400.access.AS400Bin4;
import com.ibm.as400.access.AS400Bin8;
import com.ibm.as400.access.AS400DataType;
import com.ibm.as400.access.AS400Float4;
import com.ibm.as400.access.AS400Float8;
import com.ibm.as400.access.AS400PackedDecimal;
import com.ibm.as400.access.AS400UnsignedBin2;
import com.ibm.as400.access.AS400UnsignedBin4;
import com.ibm.as400.access.AS400ZonedDecimal;
import com.ibm.as400.access.ProgramCall;
import com.ibm.as400.access.ProgramParameter;
import com.ibm.as400.access.QSYSObjectPathName;
import com.ibm.as400.access.AS400JDBCDriver;
import com.ibm.as400.access.AS400Message;
import com.ibm.as400.access.AS400Exception;
import com.ibm.as400.access.AS400SecurityException;
import com.ibm.as400.access.CharConverter;
import com.ibm.as400.access.CommandCall;
import com.ibm.as400.access.Job;
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
        Connection internal;          // only for statements this helper builds itself
        List<String> libl;            // library list for SQL and commands (null: the user's)
        CommandCall cmd;              // one remote command job, kept for the connection
        List<String> cmdLibl;         // library list currently set in that job
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
                // wrong password, expired, disabled...: the caller drops a saved password,
                // or asks for a new one when it expired (never retries the old one)
                if (e instanceof AS400SecurityException) {
                    resp.put("security", true);
                    int rc = ((AS400SecurityException) e).getReturnCode();
                    if (rc == AS400SecurityException.PASSWORD_EXPIRED) resp.put("expired", true);
                }
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
            case "change_password": return changePassword(r);
            case "command": return command(r);
            case "call_program": return callProgram(r);
            case "ifs_mkdir": {
                IFSFile d = ifs(r);
                boolean made = !d.exists() && d.mkdirs();
                if (!d.exists()) throw new IllegalArgumentException("could not create " + d.getPath());
                return made;
            }
            case "read_member": return readMember(r);
            case "spooled_file": return spooledFile(r);
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
        sys.setUseSystemPasswordExpirationWarningDays(true);   // QPWDEXPWRN
        sys.connectService(AS400.SIGNON);    // fails here on a wrong user or password
        Conn c = new Conn();
        c.system = sys;
        c.libl = strings(r.get("libl"));
        CONNS.put(name, c);
        Map<String, Object> res = new LinkedHashMap<>();
        res.put("user", sys.getUserId());
        res.put("host", sys.getSystemName());
        res.put("release", "V" + sys.getVersion() + "R" + sys.getRelease() + "M" + sys.getModification());
        res.put("tls", tls);
        try {
            if (sys.isInPasswordExpirationWarningDays())
                res.put("expires_in_days", sys.getPasswordExpirationDays());
        } catch (Exception ignored) { }
        return res;
    }

    /** Change an (expired) password. Needs the current one; nothing is retried. */
    static Object changePassword(Map<String, Object> r) throws Exception {
        boolean tls = Boolean.TRUE.equals(r.get("tls"));
        AS400 sys = tls ? new SecureAS400(str(r, "host"), str(r, "user"))
                        : new AS400(str(r, "host"), str(r, "user"));
        sys.setGuiAvailable(false);
        char[] old = str(r, "old").toCharArray(), neu = str(r, "new").toCharArray();
        try {
            sys.changePassword(old, neu);
        } finally {
            Arrays.fill(old, '\0');
            Arrays.fill(neu, '\0');
            sys.disconnectAllServices();
        }
        return true;
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
        if (c.libl != null && !c.libl.isEmpty())
            p.setProperty("libraries", String.join(",", c.libl));
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

    /** Run a CL command in the connection's remote command job (library list
     *  set first when asked for). Returns the messages, not just ok/failed. */
    static Object command(Map<String, Object> r) throws Exception {
        Conn c = conn(r);
        if (c.cmd == null) {
            c.cmd = new CommandCall(c.system);
            c.cmd.setThreadSafe(false);       // always the same server job: libl stays set
        }
        Map<String, Object> res = new LinkedHashMap<>();
        if (!setLibl(c, r, res)) return res;
        boolean ok = c.cmd.run(str(r, "command"));
        res.put("ok", ok);
        res.put("messages", messages(c.cmd.getMessageList()));
        res.put("job", jobName(c.cmd.getServerJob()));
        res.put("libl", c.cmdLibl);
        return res;
    }

    /** Set the library list in the command job when asked for (or the
     *  connection's). false (and res filled in) when CHGLIBL failed. */
    static boolean setLibl(Conn c, Map<String, Object> r, Map<String, Object> res) throws Exception {
        if (c.cmd == null) {
            c.cmd = new CommandCall(c.system);
            c.cmd.setThreadSafe(false);       // always the same server job: libl stays set
        }
        List<String> want = strings(r.get("libl"));
        if (want == null) want = c.libl;
        if (want != null && !want.isEmpty() && !want.equals(c.cmdLibl)) {
            if (!c.cmd.run("CHGLIBL LIBL(" + String.join(" ", want) + ")")) {
                res.put("ok", false);
                res.put("messages", messages(c.cmd.getMessageList()));
                res.put("step", "CHGLIBL");
                res.put("job", jobName(c.cmd.getServerJob()));
                return false;
            }
            c.cmdLibl = want;
        }
        return true;
    }

    /** One parameter type: char(n), varchar(n), packed(d,p), zoned(d,p),
     *  int(5|10|20), uns(5|10), float(4|8), ind. */
    static final class PType {
        AS400DataType dt;
        int length;
        boolean varchar, ind;
    }

    static PType ptype(String spec, AS400 sys) {
        java.util.regex.Matcher m = java.util.regex.Pattern
            .compile("(char|varchar|packed|zoned|int|uns|float|ind)(?:\\((\\d+)(?:,(\\d+))?\\))?")
            .matcher(spec.trim().toLowerCase());
        if (!m.matches()) throw new IllegalArgumentException("unknown parameter type " + spec);
        String k = m.group(1);
        int a = m.group(2) == null ? -1 : Integer.parseInt(m.group(2));
        int b = m.group(3) == null ? 0 : Integer.parseInt(m.group(3));
        PType t = new PType();
        switch (k) {
            case "char": t.dt = new com.ibm.as400.access.AS400Text(a, sys); break;
            case "ind": t.dt = new com.ibm.as400.access.AS400Text(1, sys); t.ind = true; break;
            case "varchar": t.dt = new com.ibm.as400.access.AS400Text(a, sys); t.varchar = true; break;
            case "packed": t.dt = new AS400PackedDecimal(a, b); break;
            case "zoned": t.dt = new AS400ZonedDecimal(a, b); break;
            case "int": t.dt = a == 5 ? new AS400Bin2() : a == 20 ? new AS400Bin8() : new AS400Bin4(); break;
            case "uns": t.dt = a == 5 ? new AS400UnsignedBin2() : new AS400UnsignedBin4(); break;
            case "float": t.dt = a == 4 ? new AS400Float4() : new AS400Float8(); break;
            default: throw new IllegalArgumentException("unknown parameter type " + spec);
        }
        t.length = t.dt.getByteLength() + (t.varchar ? 2 : 0);
        return t;
    }

    static byte[] toBytes(PType t, Object v) {
        if (t.varchar) {
            String s = v == null ? "" : String.valueOf(v);
            byte[] text = t.dt.toBytes(s);
            int n = Math.min(s.length(), t.dt.getByteLength());
            byte[] out = new byte[t.length];
            out[0] = (byte) (n >> 8);
            out[1] = (byte) n;
            System.arraycopy(text, 0, out, 2, text.length);
            return out;
        }
        if (t.ind) return t.dt.toBytes(Boolean.TRUE.equals(v) || "1".equals(String.valueOf(v)) ? "1" : "0");
        if (t.dt instanceof com.ibm.as400.access.AS400Text) return t.dt.toBytes(v == null ? "" : String.valueOf(v));
        String s = v == null ? "0" : String.valueOf(v).trim();
        if (t.dt instanceof AS400PackedDecimal || t.dt instanceof AS400ZonedDecimal)
            return t.dt.toBytes(new BigDecimal(s));
        if (t.dt instanceof AS400Bin2) return t.dt.toBytes(Short.valueOf(s));
        if (t.dt instanceof AS400Bin4) return t.dt.toBytes(Integer.valueOf(s));
        if (t.dt instanceof AS400Bin8) return t.dt.toBytes(Long.valueOf(s));
        if (t.dt instanceof AS400UnsignedBin2) return t.dt.toBytes(Integer.valueOf(s));
        if (t.dt instanceof AS400UnsignedBin4) return t.dt.toBytes(Long.valueOf(s));
        if (t.dt instanceof AS400Float4) return t.dt.toBytes(Float.valueOf(s));
        return t.dt.toBytes(Double.valueOf(s));
    }

    static String fromBytes(PType t, byte[] b) {
        if (t.varchar) {
            int n = ((b[0] & 0xFF) << 8) | (b[1] & 0xFF);
            byte[] text = Arrays.copyOfRange(b, 2, 2 + Math.min(n, b.length - 2));
            return (String) new com.ibm.as400.access.AS400Text(text.length,
                ((com.ibm.as400.access.AS400Text) t.dt).getCcsid()).toObject(text);
        }
        Object o = t.dt.toObject(b);
        if (o instanceof BigDecimal) return ((BigDecimal) o).toPlainString();
        return rtrim(String.valueOf(o));
    }

    /** CALL a program with typed parameters in the command job (same library
     *  list as run_command); returns the output parameters as text. */
    static Object callProgram(Map<String, Object> r) throws Exception {
        Conn c = conn(r);
        Map<String, Object> res = new LinkedHashMap<>();
        if (!setLibl(c, r, res)) return res;
        String prog = str(r, "program").toUpperCase();
        String lib = "*LIBL", name = prog;
        if (prog.contains("/")) { lib = prog.substring(0, prog.indexOf('/')); name = prog.substring(prog.indexOf('/') + 1); }
        List<?> spec = r.get("params") instanceof List ? (List<?>) r.get("params") : new ArrayList<>();
        PType[] types = new PType[spec.size()];
        ProgramParameter[] pp = new ProgramParameter[spec.size()];
        for (int i = 0; i < spec.size(); i++) {
            @SuppressWarnings("unchecked")
            Map<String, Object> p = (Map<String, Object>) spec.get(i);
            types[i] = ptype(str(p, "type"), c.system);
            byte[] in = toBytes(types[i], p.get("value"));
            pp[i] = Boolean.TRUE.equals(p.get("out")) ? new ProgramParameter(in, types[i].length)
                                                     : new ProgramParameter(in);
        }
        ProgramCall pc = new ProgramCall(c.system);
        pc.setThreadSafe(false);              // the command job: its library list
        boolean ok = pc.run(new QSYSObjectPathName(lib, name, "PGM").getPath(), pp);
        List<Object> outs = new ArrayList<>();
        for (int i = 0; i < pp.length; i++) {
            byte[] o = pp[i].getOutputData();
            outs.add(ok && o != null ? fromBytes(types[i], o) : null);
        }
        res.put("ok", ok);
        res.put("outputs", outs);
        res.put("messages", messages(pc.getMessageList()));
        res.put("job", jobName(c.cmd.getServerJob()));
        return res;
    }

    static String jobName(Job j) {
        return j.getNumber() + "/" + j.getUser().trim() + "/" + j.getName().trim();
    }

    static List<Object> messages(AS400Message[] list) {
        List<Object> out = new ArrayList<>();
        if (list == null) return out;
        for (AS400Message m : list) {
            Map<String, Object> mm = new LinkedHashMap<>();
            mm.put("id", m.getID());
            mm.put("severity", m.getSeverity());
            mm.put("type", m.getType());
            mm.put("text", m.getText());
            out.add(mm);
        }
        return out;
    }

    static List<String> strings(Object o) {
        if (!(o instanceof List)) return null;
        List<String> out = new ArrayList<>();
        for (Object x : (List<?>) o) out.add(String.valueOf(x));
        return out;
    }

    /** A connection for this helper's own fixed statements (aliases in QTEMP,
     *  spooled file reads); never for SQL that comes from the caller. */
    static Connection internal(Conn c) throws SQLException {
        if (c.internal != null && !c.internal.isClosed()) return c.internal;
        Properties p = new Properties();
        p.setProperty("naming", "system");
        p.setProperty("errors", "full");
        p.setProperty("prompt", "false");
        // e.g. compiler event files are CCSID 65535: read them as text, not hex
        p.setProperty("translate binary", "true");
        c.internal = new AS400JDBCDriver().connect(c.system, p, null);
        c.internal.setAutoCommit(true);
        return c.internal;
    }

    static String sysName(Map<String, Object> r, String k) {
        String v = str(r, k).toUpperCase();
        if (!v.matches("[A-Z$#@][A-Z0-9_$#@.]{0,9}"))
            throw new IllegalArgumentException("not an object name: " + v);
        return v;
    }

    /** Records of a database file member (e.g. a compiler event file). */
    static Object readMember(Map<String, Object> r) throws Exception {
        Conn c = conn(r);
        String lib = sysName(r, "lib"), file = sysName(r, "file"), mbr = sysName(r, "member");
        int max = num(r, "max_records", 20000);
        List<Object> recs = new ArrayList<>();
        try (Statement st = internal(c).createStatement()) {
            st.execute("CREATE OR REPLACE ALIAS QTEMP/T5250MBR FOR " + lib + "/" + file + " (" + mbr + ")");
            try (ResultSet rs = st.executeQuery("SELECT * FROM QTEMP/T5250MBR")) {
                int n = rs.getMetaData().getColumnCount();
                while (rs.next() && recs.size() < max) {
                    StringBuilder sb = new StringBuilder();
                    for (int i = 1; i <= n; i++) {
                        String v = rs.getString(i);
                        if (v != null) sb.append(v);
                    }
                    recs.add(rtrim(sb.toString()));
                }
            }
        }
        Map<String, Object> res = new LinkedHashMap<>();
        res.put("records", recs);
        return res;
    }

    /** Lines of a spooled file (SYSTOOLS.SPOOLED_FILE_DATA). */
    static Object spooledFile(Map<String, Object> r) throws Exception {
        Conn c = conn(r);
        int max = num(r, "max_lines", 5000);
        List<Object> lines = new ArrayList<>();
        try (PreparedStatement ps = internal(c).prepareStatement(
                "SELECT SPOOLED_DATA FROM TABLE(SYSTOOLS.SPOOLED_FILE_DATA("
                + "JOB_NAME => CAST(? AS VARCHAR(28)), SPOOLED_FILE_NAME => CAST(? AS VARCHAR(10)), "
                + "SPOOLED_FILE_NUMBER => CAST(? AS VARCHAR(6)))) X ORDER BY ORDINAL_POSITION")) {
            ps.setString(1, str(r, "job"));
            ps.setString(2, str(r, "file").toUpperCase());
            ps.setString(3, String.valueOf(r.getOrDefault("number", "*LAST")));
            try (ResultSet rs = ps.executeQuery()) {
                while (rs.next() && lines.size() < max) {
                    String v = rs.getString(1);
                    lines.add(v == null ? "" : rtrim(v));
                }
            }
        }
        Map<String, Object> res = new LinkedHashMap<>();
        res.put("lines", lines);
        res.put("truncated", lines.size() >= max);
        return res;
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
        for (Connection x : new Connection[]{c.readOnly, c.readWrite, c.internal}) {
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
