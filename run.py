#!/usr/bin/env python3

# Script to prepare live IPS namespace labs for Suricata testing.
#
# Modes:
#   afp - AF_PACKET inline-style topology with two separate DUT interfaces.
#         No Linux bridge is created; Suricata is expected to move traffic
#         between the DUT interfaces.
#
#   nfq - Routed topology for NFQUEUE IPS testing. The DUT routes packets
#         between client and server networks, and forwarded packets are sent
#         to NFQUEUE so userspace must verdict them.
#
# Namespaces:
#   client
#   server
#   dut
#
# Usage:
#   ./run.py afp up
#   ./run.py afp down
#   ./run.py afp status
#   ./run.py afp shell [client|server|dut]
#
#   ./run.py nfq up
#   ./run.py nfq down
#   ./run.py nfq status
#   ./run.py nfq shell [client|server|dut]

import os
import shutil
import subprocess
import sys
import time

CLIENT_NS = "client"
SERVER_NS = "server"
DUT_NS = "dut"

MTU = "1500"

CLIENT_IF = "client"
SERVER_IF = "server"
DUT_CLIENT_IF = "client0"
DUT_SERVER_IF = "server0"

TMP_CLIENT_IF = "ptp-client"
TMP_SERVER_IF = "ptp-server"
TMP_DUT_CLIENT_IF = "ptp-client0"
TMP_DUT_SERVER_IF = "ptp-server0"

AFP_CLIENT_IP = "10.200.0.2/24"
AFP_SERVER_IP = "10.200.0.1/24"

NFQ_CLIENT_IP = "10.200.2.2/24"
NFQ_DUT_CLIENT_IP = "10.200.2.254/24"
NFQ_SERVER_IP = "10.200.1.1/24"
NFQ_DUT_SERVER_IP = "10.200.1.254/24"
NFQ_CLIENT_GW = "10.200.2.254"
NFQ_SERVER_GW = "10.200.1.254"
NFQ_QUEUE_NUM = "0"

ALL_NAMESPACES = (CLIENT_NS, SERVER_NS, DUT_NS)
ROOT_LINKS = (TMP_CLIENT_IF, TMP_SERVER_IF, TMP_DUT_CLIENT_IF, TMP_DUT_SERVER_IF)
MODES = ("afp", "nfq")


def usage() -> None:
    print(
        f"Usage: {sys.argv[0]} <afp|nfq> <up|down|status|shell> [client|server|dut]",
        file=sys.stderr,
    )


def need_root() -> None:
    if os.geteuid() == 0:
        return
    os.execvp("sudo", ["sudo", sys.executable, *sys.argv])


def need_cmd(cmd: str) -> None:
    if shutil.which(cmd) is None:
        print(f"ERROR: missing command: {cmd}", file=sys.stderr)
        sys.exit(1)


def run(cmd: list[str], *, quiet: bool = False, capture: bool = False) -> subprocess.CompletedProcess[str]:
    kwargs: dict[str, object] = {
        "check": True,
        "text": True,
    }
    if quiet:
        kwargs["stdout"] = subprocess.DEVNULL
        kwargs["stderr"] = subprocess.DEVNULL
    elif capture:
        kwargs["capture_output"] = True
    return subprocess.run(cmd, **kwargs)


def try_run(cmd: list[str]) -> None:
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


def show(cmd: list[str]) -> None:
    subprocess.run(cmd, check=False)


def ns_exec(ns: str, cmd: list[str], *, quiet: bool = False, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return run(["ip", "netns", "exec", ns, *cmd], quiet=quiet, capture=capture)


def ns_try(ns: str, cmd: list[str]) -> None:
    try_run(["ip", "netns", "exec", ns, *cmd])


def ns_show(ns: str, cmd: list[str]) -> None:
    show(["ip", "netns", "exec", ns, *cmd])


def netns_exists(ns: str) -> bool:
    result = run(["ip", "netns", "list"], capture=True)
    return any(line.split() and line.split()[0] == ns for line in result.stdout.splitlines())


def kill_ns_processes(ns: str) -> None:
    if not netns_exists(ns):
        return

    result = run(["ip", "netns", "pids", ns], capture=True)
    pids = [pid for pid in result.stdout.split() if pid.isdigit()]
    if not pids:
        return

    try_run(["kill", *pids])
    time.sleep(0.1)

    result = run(["ip", "netns", "pids", ns], capture=True)
    pids = [pid for pid in result.stdout.split() if pid.isdigit()]
    if pids:
        try_run(["kill", "-9", *pids])


def disable_offloads(ns: str, iface: str) -> None:
    for feature in ("rx", "tx", "tso", "gro", "lro", "gso", "sg", "rxvlan", "txvlan"):
        ns_try(ns, ["ethtool", "-K", iface, feature, "off"])


def setup_namespaces() -> None:
    for ns in ALL_NAMESPACES:
        run(["ip", "netns", "add", ns])
        run(["ip", "-n", ns, "link", "set", "lo", "up"])
        ns_exec(ns, ["sysctl", "-w", "net.ipv4.ping_group_range=0 2147483647"], quiet=True)


def setup_links() -> None:
    run(["ip", "link", "add", TMP_CLIENT_IF, "type", "veth", "peer", "name", TMP_DUT_CLIENT_IF])
    run(["ip", "link", "add", TMP_SERVER_IF, "type", "veth", "peer", "name", TMP_DUT_SERVER_IF])

    run(["ip", "link", "set", TMP_CLIENT_IF, "netns", CLIENT_NS])
    run(["ip", "link", "set", TMP_SERVER_IF, "netns", SERVER_NS])
    run(["ip", "link", "set", TMP_DUT_CLIENT_IF, "netns", DUT_NS])
    run(["ip", "link", "set", TMP_DUT_SERVER_IF, "netns", DUT_NS])

    run(["ip", "-n", CLIENT_NS, "link", "set", TMP_CLIENT_IF, "name", CLIENT_IF])
    run(["ip", "-n", SERVER_NS, "link", "set", TMP_SERVER_IF, "name", SERVER_IF])
    run(["ip", "-n", DUT_NS, "link", "set", TMP_DUT_CLIENT_IF, "name", DUT_CLIENT_IF])
    run(["ip", "-n", DUT_NS, "link", "set", TMP_DUT_SERVER_IF, "name", DUT_SERVER_IF])

    for ns, iface in (
        (CLIENT_NS, CLIENT_IF),
        (SERVER_NS, SERVER_IF),
        (DUT_NS, DUT_CLIENT_IF),
        (DUT_NS, DUT_SERVER_IF),
    ):
        run(["ip", "-n", ns, "link", "set", iface, "mtu", MTU])


def bring_up_interface(ns: str, iface: str) -> None:
    disable_offloads(ns, iface)
    ns_exec(ns, ["ip", "link", "set", iface, "up"])


def add_address(ns: str, iface: str, cidr: str) -> None:
    ns_exec(ns, ["ip", "addr", "add", cidr, "dev", iface])


def replace_default_route(ns: str, via: str) -> None:
    ns_exec(ns, ["ip", "route", "replace", "default", "via", via])


def setup_common_topology() -> None:
    do_down(quiet=True)
    setup_namespaces()
    setup_links()


def afp_up() -> None:
    setup_common_topology()

    add_address(CLIENT_NS, CLIENT_IF, AFP_CLIENT_IP)
    add_address(SERVER_NS, SERVER_IF, AFP_SERVER_IP)

    bring_up_interface(CLIENT_NS, CLIENT_IF)
    bring_up_interface(SERVER_NS, SERVER_IF)
    bring_up_interface(DUT_NS, DUT_CLIENT_IF)
    bring_up_interface(DUT_NS, DUT_SERVER_IF)

    print("AF_PACKET lab is up.")
    print(f"  client namespace: {CLIENT_NS}")
    print(f"  server namespace: {SERVER_NS}")
    print(f"  dut namespace:    {DUT_NS}")
    print()
    print("Interfaces:")
    print(f"  {CLIENT_NS}: {CLIENT_IF} ({AFP_CLIENT_IP})")
    print(f"  {DUT_NS}:    {DUT_CLIENT_IF}")
    print(f"  {DUT_NS}:    {DUT_SERVER_IF}")
    print(f"  {SERVER_NS}: {SERVER_IF} ({AFP_SERVER_IP})")
    print()
    print("Traffic path for AF_PACKET inline testing:")
    print(
        f"  {CLIENT_NS}:{CLIENT_IF} -> {DUT_NS}:{DUT_CLIENT_IF} ... Suricata ... "
        f"{DUT_NS}:{DUT_SERVER_IF} -> {SERVER_NS}:{SERVER_IF}"
    )
    print()
    print("Notes:")
    print("  - No Linux bridge is created.")
    print("  - Client and server can only communicate once something in the DUT forwards traffic between its two interfaces.")


def setup_nfq_iptables() -> None:
    ns_exec(DUT_NS, ["iptables", "-F"])
    ns_exec(DUT_NS, ["iptables", "-P", "FORWARD", "DROP"])
    ns_exec(DUT_NS, ["iptables", "-A", "FORWARD", "-i", DUT_CLIENT_IF, "-o", DUT_SERVER_IF, "-j", "NFQUEUE", "--queue-num", NFQ_QUEUE_NUM])
    ns_exec(DUT_NS, ["iptables", "-A", "FORWARD", "-i", DUT_CLIENT_IF, "-o", DUT_SERVER_IF, "-j", "ACCEPT"])
    ns_exec(DUT_NS, ["iptables", "-A", "FORWARD", "-i", DUT_SERVER_IF, "-o", DUT_CLIENT_IF, "-j", "NFQUEUE", "--queue-num", NFQ_QUEUE_NUM])
    ns_exec(DUT_NS, ["iptables", "-A", "FORWARD", "-i", DUT_SERVER_IF, "-o", DUT_CLIENT_IF, "-j", "ACCEPT"])


def nfq_up() -> None:
    setup_common_topology()

    add_address(CLIENT_NS, CLIENT_IF, NFQ_CLIENT_IP)
    add_address(SERVER_NS, SERVER_IF, NFQ_SERVER_IP)
    add_address(DUT_NS, DUT_CLIENT_IF, NFQ_DUT_CLIENT_IP)
    add_address(DUT_NS, DUT_SERVER_IF, NFQ_DUT_SERVER_IP)

    bring_up_interface(CLIENT_NS, CLIENT_IF)
    bring_up_interface(SERVER_NS, SERVER_IF)
    bring_up_interface(DUT_NS, DUT_CLIENT_IF)
    bring_up_interface(DUT_NS, DUT_SERVER_IF)

    replace_default_route(CLIENT_NS, NFQ_CLIENT_GW)
    replace_default_route(SERVER_NS, NFQ_SERVER_GW)

    ns_exec(DUT_NS, ["sysctl", "-w", "net.ipv4.ip_forward=1"], quiet=True)
    setup_nfq_iptables()

    print("NFQ lab is up.")
    print(f"  client namespace: {CLIENT_NS}")
    print(f"  server namespace: {SERVER_NS}")
    print(f"  dut namespace:    {DUT_NS}")
    print()
    print("Interfaces:")
    print(f"  {CLIENT_NS}: {CLIENT_IF} ({NFQ_CLIENT_IP})")
    print(f"  {DUT_NS}:    {DUT_CLIENT_IF} ({NFQ_DUT_CLIENT_IP})")
    print(f"  {DUT_NS}:    {DUT_SERVER_IF} ({NFQ_DUT_SERVER_IP})")
    print(f"  {SERVER_NS}: {SERVER_IF} ({NFQ_SERVER_IP})")
    print()
    print("Routing path for NFQUEUE IPS testing:")
    print(
        f"  {CLIENT_NS}:{CLIENT_IF} -> {DUT_NS}:{DUT_CLIENT_IF} -> routing/NFQUEUE -> "
        f"{DUT_NS}:{DUT_SERVER_IF} -> {SERVER_NS}:{SERVER_IF}"
    )
    print()
    print("Notes:")
    print(f"  - The DUT queues forwarded packets to NFQUEUE {NFQ_QUEUE_NUM}.")
    print("  - Packets require a userspace verdict, e.g. from Suricata, before they will be forwarded.")


def do_down(*, quiet: bool = False) -> None:
    for ns in ALL_NAMESPACES:
        kill_ns_processes(ns)

    for link in ROOT_LINKS:
        try_run(["ip", "link", "del", link])

    for ns in ALL_NAMESPACES:
        try_run(["ip", "netns", "del", ns])

    if not quiet:
        print("Lab torn down.")


def show_common_status() -> None:
    print("Namespaces:")
    show(["ip", "netns", "list"])

    print(f"\n{CLIENT_NS} namespace:")
    ns_show(CLIENT_NS, ["ip", "addr"])
    ns_show(CLIENT_NS, ["ip", "route"])

    print(f"\n{SERVER_NS} namespace:")
    ns_show(SERVER_NS, ["ip", "addr"])
    ns_show(SERVER_NS, ["ip", "route"])

    print(f"\n{DUT_NS} namespace:")
    ns_show(DUT_NS, ["ip", "addr"])
    ns_show(DUT_NS, ["ip", "route"])


def afp_status() -> None:
    print("Mode: afp")
    show_common_status()


def nfq_status() -> None:
    print("Mode: nfq")
    show_common_status()
    print(f"\n{DUT_NS} forwarding:")
    ns_show(DUT_NS, ["sysctl", "net.ipv4.ip_forward"])
    print(f"\n{DUT_NS} iptables FORWARD rules:")
    ns_show(DUT_NS, ["iptables", "-S", "FORWARD"])


def do_shell(target: str) -> None:
    namespaces = {
        "client": CLIENT_NS,
        "server": SERVER_NS,
        "dut": DUT_NS,
    }
    if target not in namespaces:
        print("ERROR: shell target must be one of: client, server, dut", file=sys.stderr)
        sys.exit(1)
    os.execvp("ip", ["ip", "netns", "exec", namespaces[target], "bash"])


def main() -> None:
    if len(sys.argv) < 3:
        usage()
        sys.exit(1)

    mode = sys.argv[1]
    action = sys.argv[2]

    if mode not in MODES:
        usage()
        sys.exit(1)

    need_root()

    required_cmds = ["ip", "ethtool", "sysctl", "kill"]
    if mode == "nfq":
        required_cmds.append("iptables")
    for cmd in required_cmds:
        need_cmd(cmd)

    if action == "up":
        if mode == "afp":
            afp_up()
        else:
            nfq_up()
    elif action == "down":
        do_down()
    elif action == "status":
        if mode == "afp":
            afp_status()
        else:
            nfq_status()
    elif action == "shell":
        target = sys.argv[3] if len(sys.argv) > 3 else "client"
        do_shell(target)
    else:
        usage()
        sys.exit(1)


if __name__ == "__main__":
    main()
