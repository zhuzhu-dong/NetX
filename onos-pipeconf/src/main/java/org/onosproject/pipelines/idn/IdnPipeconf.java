package org.onosproject.pipelines.idn;

import org.onosproject.net.pi.model.DefaultPiPipeconf;
import org.onosproject.net.pi.model.PiPipeconf;
import org.onosproject.net.pi.model.PiPipeconf.ExtensionType;
import org.onosproject.net.pi.model.PiPipeconfId;
import org.onosproject.net.behaviour.Pipeliner;
import org.onosproject.net.pi.model.PiPipelineInterpreter;
import org.onosproject.net.pi.service.PiPipeconfService;
import org.onosproject.p4runtime.model.P4InfoParser;
import org.onosproject.p4runtime.model.P4InfoParserException;
import org.osgi.service.component.annotations.Activate;
import org.osgi.service.component.annotations.Component;
import org.osgi.service.component.annotations.Deactivate;
import org.osgi.service.component.annotations.Reference;
import org.osgi.service.component.annotations.ReferenceCardinality;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.net.URL;

@Component(immediate = true)
public class IdnPipeconf {

    private static final Logger log = LoggerFactory.getLogger(IdnPipeconf.class);

    public static final PiPipeconfId PID =
            new PiPipeconfId("org.onosproject.pipelines.idn-basic");

    @Reference(cardinality = ReferenceCardinality.MANDATORY)
    private PiPipeconfService pipeconfService;

    @Activate
    public void activate() {
        URL p4InfoUrl = getClass().getClassLoader().getResource("idn_basic.p4info.txt");
        URL jsonUrl   = getClass().getClassLoader().getResource("idn_basic.json");

        if (p4InfoUrl == null || jsonUrl == null) {
            throw new IllegalStateException(
                    "P4 artifacts not found: p4info=" + p4InfoUrl + ", json=" + jsonUrl);
        }

        PiPipeconf pipeconf;
        try {
            pipeconf = DefaultPiPipeconf.builder()
                    .withId(PID)
                    .withPipelineModel(P4InfoParser.parse(p4InfoUrl))
                    .addExtension(ExtensionType.P4_INFO_TEXT, p4InfoUrl)
                    .addExtension(ExtensionType.BMV2_JSON, jsonUrl)
                    .addBehaviour(PiPipelineInterpreter.class, IdnInterpreter.class)
                    .addBehaviour(Pipeliner.class, IdnPipeliner.class)
                    .build();
        } catch (P4InfoParserException e) {
            throw new IllegalStateException("Failed to parse P4Info", e);
        }

        pipeconfService.register(pipeconf);
        log.info("IDN pipeconf registered: {}", PID);
    }

    @Deactivate
    public void deactivate() {
        pipeconfService.unregister(PID);
        log.info("IDN pipeconf unregistered: {}", PID);
    }
}
