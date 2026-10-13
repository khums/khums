import java.io.BufferedReader;
import java.io.FileDescriptor;
import java.io.FileOutputStream;
import java.io.InputStreamReader;
import java.io.PrintStream;
import java.io.Serializable;
import java.lang.reflect.InvocationHandler;
import java.lang.reflect.Method;
import java.lang.reflect.Proxy;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.List;

import org.apache.mina.core.buffer.IoBuffer;

/**
 * Fixture-free observation worker for the private verifier.
 *
 * Each stdin line is one case: mode TAB accept-patterns ('|'-separated) TAB items
 * (','-separated). It serializes the described value with IoBuffer, deserializes it
 * through the submitted IoBuffer with the given accept patterns, and prints only
 * what it observed. Expected outcomes and pass/fail decisions live in the trusted
 * verifier process; nothing here encodes an expectation.
 */
public final class ProxyWorker {

    static final class Handler implements InvocationHandler, Serializable {
        private static final long serialVersionUID = 1L;

        @Override
        public Object invoke(Object proxy, Method method, Object[] args) {
            return null;
        }
    }

    public static void main(String[] args) throws Exception {
        BufferedReader in = new BufferedReader(new InputStreamReader(System.in, StandardCharsets.UTF_8));
        PrintStream out = new PrintStream(new FileOutputStream(FileDescriptor.out), true, "UTF-8");
        String line;
        while ((line = in.readLine()) != null) {
            if (!line.isEmpty()) {
                out.println(observe(line.split("\t", -1)));
            }
        }
    }

    private static String[] list(String field, String separator) {
        return field.isEmpty() ? new String[0] : field.split(separator);
    }

    private static String observe(String[] fields) {
        String mode = fields[0];
        String[] patterns = list(fields[1], "\\|");
        String[] items = list(fields[2], ",");
        ClassLoader loader = ProxyWorker.class.getClassLoader();
        IoBuffer buffer;
        try {
            Object value;
            if (mode.equals("list")) {
                value = new ArrayList<>(Arrays.asList(items));
            } else {
                Class<?>[] interfaces = new Class<?>[items.length];
                for (int i = 0; i < items.length; i++) {
                    interfaces[i] = Class.forName(items[i], false, loader);
                }
                Object proxy = Proxy.newProxyInstance(loader, interfaces, new Handler());
                value = mode.equals("proxy_in_list") ? new ArrayList<>(Collections.singletonList(proxy)) : proxy;
            }
            buffer = IoBuffer.allocate(64).setAutoExpand(true);
            buffer.putObject(value);
            buffer.flip();
            buffer.accept(patterns);
        } catch (Throwable setup) {
            return "ERROR\tsetup:" + setup.getClass().getName();
        }

        Object decoded;
        try {
            decoded = buffer.getObject(loader);
        } catch (Throwable rejected) {
            return "REJECTED\t" + rejected.getClass().getName();
        }

        try {
            if (mode.equals("list")) {
                List<String> values = new ArrayList<>();
                for (Object element : (List<?>) decoded) {
                    values.add(String.valueOf(element));
                }
                return "VALUE\t" + String.join(",", values);
            }
            Object candidate = mode.equals("proxy_in_list") ? ((List<?>) decoded).get(0) : decoded;
            if (!Proxy.isProxyClass(candidate.getClass())) {
                return "ACCEPTED\tfalse\t";
            }
            List<String> names = new ArrayList<>();
            for (Class<?> type : candidate.getClass().getInterfaces()) {
                names.add(type.getName());
            }
            Collections.sort(names);
            return "ACCEPTED\ttrue\t" + String.join(",", names);
        } catch (Throwable inspect) {
            return "ERROR\tinspect:" + inspect.getClass().getName();
        }
    }
}
