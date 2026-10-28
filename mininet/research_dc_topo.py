#!/usr/bin/env python3
"""
research_dc_topo.py - IDN-System Mininet/BMv2 topology entry point.

Topology:
    h_research/h_video - s_access_r - s_agg_r
                                  - s_core1 - s_agg_d - s_access_d - h_dc/h_backup
                                  - s_core2 ----/
    h_ai -------------- s_agg_r
    s_agg_r ----------- s_agg_d (backup, 5Gbps, 5ms)

The topology is defined in mininet/topologies/research_dc.yaml — the single
source of truth.  All switch/host/link config is derived from that file by
mininet/topo_loader.py.

Usage:
    sudo python3 research_dc_topo.py --onos-ip 127.0.0.1
    sudo python3 research_dc_topo.py --onos-ip 172.17.0.2 --p4-dir ../p4/build
    sudo python3 research_dc_topo.py --topo my_new_topo   # use different topology
    python3 research_dc_topo.py --list                    # list available topologies
"""

import sys
sys.path.insert(0, '/app')

from mn.topo_loader import run_topo, main

if __name__ == "__main__":
    main()
