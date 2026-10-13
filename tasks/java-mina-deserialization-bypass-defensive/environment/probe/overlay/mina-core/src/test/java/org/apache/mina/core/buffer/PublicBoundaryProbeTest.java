package org.apache.mina.core.buffer;

import java.io.Serializable;
import java.lang.reflect.InvocationHandler;
import java.lang.reflect.Method;
import java.lang.reflect.Proxy;
import org.junit.Test;
import static org.junit.Assert.assertTrue;

public class PublicBoundaryProbeTest {
    interface Marker { }

    static final class Handler implements InvocationHandler, Serializable {
        private static final long serialVersionUID = 1L;
        @Override public Object invoke(Object proxy, Method method, Object[] args) {
            return null;
        }
    }

    private IoBuffer encodedProxy() throws Exception {
        Object value = Proxy.newProxyInstance(getClass().getClassLoader(), new Class<?>[] { Marker.class }, new Handler());
        IoBuffer buffer = IoBuffer.allocate(64).setAutoExpand(true);
        buffer.putObject(value);
        buffer.flip();
        return buffer;
    }

    @Test(expected = ClassNotFoundException.class)
    public void unacceptedProxyInterfaceIsRejected() throws Exception {
        IoBuffer buffer = encodedProxy();
        buffer.accept(Proxy.class.getName(), Handler.class.getName());
        buffer.getObject(getClass().getClassLoader());
    }

    @Test
    public void acceptedProxyInterfaceStillWorks() throws Exception {
        IoBuffer buffer = encodedProxy();
        buffer.accept(Proxy.class.getName(), Handler.class.getName(), Marker.class.getName());
        assertTrue(Proxy.isProxyClass(buffer.getObject(getClass().getClassLoader()).getClass()));
    }
}
