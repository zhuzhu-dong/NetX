package org.onosproject.pipelines.idn;

import java.util.List;
import org.onosproject.core.ApplicationId;
import org.onosproject.net.DeviceId;
import org.onosproject.net.behaviour.NextGroup;
import org.onosproject.net.behaviour.Pipeliner;
import org.onosproject.net.behaviour.PipelinerContext;
import org.onosproject.net.driver.DriverData;
import org.onosproject.net.driver.DriverHandler;
import org.onosproject.net.flowobjective.FilteringObjective;
import org.onosproject.net.flowobjective.ForwardingObjective;
import org.onosproject.net.flowobjective.NextObjective;
import org.onosproject.net.flowobjective.ObjectiveError;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

public class IdnPipeliner implements Pipeliner {

    private static final Logger log = LoggerFactory.getLogger(IdnPipeliner.class);

    private DriverData data;
    private DriverHandler handler;
    private DeviceId deviceId;
    private PipelinerContext context;

    @Override
    public DriverData data() { return data; }

    @Override
    public void setData(DriverData data) { this.data = data; }

    @Override
    public DriverHandler handler() { return handler; }

    @Override
    public void setHandler(DriverHandler handler) { this.handler = handler; }

    @Override
    public void init(DeviceId deviceId, PipelinerContext context) {
        this.deviceId = deviceId;
        this.context = context;
        log.info("IdnPipeliner initialized for {}", deviceId);
    }

    @Override
    public void filter(FilteringObjective filterObj) {
        filterObj.context().ifPresent(ctx -> ctx.onSuccess(filterObj));
    }

    @Override
    public void forward(ForwardingObjective fwd) {
        log.debug("Forward obj: flag={} op={} selector={}",
                fwd.flag(), fwd.op(), fwd.selector());
        // P4 Path B unconditionally punts non-IPv4 (LLDP, BDDP, ARP) to CPU.
        // P4 Path C forwards IPv4 through ipv4_routing table (LPM + ECMP).
        // Accept all objectives — the P4 data plane already does the work.
        fwd.context().ifPresent(ctx -> ctx.onSuccess(fwd));
    }

    @Override
    public void next(NextObjective nextObj) {
        // Action selector group/member are managed by the Python controller
        // via P4Runtime (REST API). The Pipeliner simply accepts — the
        // ONOS group subsystem handles the lifecycle.
        nextObj.context().ifPresent(ctx -> ctx.onSuccess(nextObj));
    }

    @Override
    public void purgeAll(ApplicationId appId) {
        // No flow rules are written to the switch, so nothing to purge.
    }

    @Override
    public List<String> getNextMappings(NextGroup nextGroup) {
        return List.of();
    }
}
