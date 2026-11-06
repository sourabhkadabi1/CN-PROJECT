# SDN-Based Distributed TCP Backup System

A fault-tolerant file backup prototype that transfers files in chunks to two backup servers while an SDN controller manages the network path between the client and the servers.

The project combines a TCP application-layer backup protocol with an OpenFlow 1.3 network built using Mininet and Open vSwitch.

## 1. Project Overview

### Problem

Sending a file to only one backup server creates a single point of failure. A network-link failure or server interruption can also interrupt a transfer.

### Our Approach

This project sends a file to two backup servers and verifies the stored copies using SHA-256 checksums.

The network has two alternative paths:

- **Path A:** Higher bandwidth and lower delay (10 Mbps, 5 ms per core link).
- **Path B:** Lower bandwidth and higher delay (4 Mbps, 30 ms per core link).

The SDN controller installs forwarding rules and, in dynamic mode, monitors link statistics and can move traffic to an alternative path when a link fails or another path becomes sufficiently better.

### Main Capabilities

- Splits files into 64 KB chunks.
- Sends backups to two destination servers.
- Uses SHA-256 checksums to verify data integrity.
- Supports resuming interrupted transfers.
- Detects corrupted chunks and allows retransmission.
- Supports a no-resume mode for comparison.
- Provides static and dynamic SDN routing modes.
- Includes Mininet experiments for link failure, throughput, and recovery measurements.
- Includes automated client/server integration tests.

> **Scope note:** The backup client/server and SDN controller are separate components that run together in the test network. This version does not implement a direct application-to-controller API.

## 2. Architecture

```text
                         SDN Network (OpenFlow 1.3)

                         +----- s2 -----+
                         |    Path A    |
h1 (TCP client) --- s1 --+              +-- s4 --- h2 (Backup Server 1)
                         |    Path B    |          h3 (Backup Server 2)
                         +----- s3 -----+

                  SDN Controller (OS-Ken) manages s1-s4
```

| Component | Responsibility |
|---|---|
| `client/` | Reads files, splits them into chunks, sends data to backup servers, handles retries and resume. |
| `server/` | Receives chunks, stores data, tracks received chunks, and verifies file integrity. |
| `protocol/` | Defines shared message formats and protocol structures. |
| `controller/` | Implements OS-Ken SDN controller and OpenFlow 1.3 forwarding logic. |
| `topology/` | Builds the Mininet topology containing hosts and switches. |
| `utils/` | Provides checksum utilities. |
| `experiments/` | Runs link-failure measurements and generates graphs. |
| `tests/` | Contains automated client/server integration tests. |

## 3. Requirements

The network-emulation setup is intended for Ubuntu Linux.

### System Requirements

- Ubuntu Linux
- Python 3
- Mininet
- Open vSwitch
- OpenFlow 1.3 support
- OS-Ken (`os-ken`)
- Matplotlib
- iperf (optional manual throughput testing)

### Install System Packages

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git mininet openvswitch-switch iperf
```

Check Mininet and Open vSwitch:

```bash
mn --version
ovs-vsctl --version
```

Start Open vSwitch:

```bash
sudo systemctl enable --now openvswitch-switch
```

### Set Up Python Dependencies

From the repository root:

```bash
python3 -m venv sdn-env
source sdn-env/bin/activate

python -m pip install --upgrade pip
python -m pip install os-ken matplotlib
```

Check dependencies:

```bash
python -c "import os_ken, matplotlib; print('Python dependencies OK')"
sudo python3 -c "import mininet; print('Mininet OK')"
```

If the Mininet import fails, repair the Mininet installation before continuing.

## 4. Get the Project

Clone the GitHub repository:

```bash
git clone <YOUR-GITHUB-REPOSITORY-URL>
cd <REPOSITORY-FOLDER>
```

Replace the placeholders with your actual GitHub repository URL and folder name.

Run commands from the directory containing `client/`, `server/`, `controller/`, `topology/`, and `experiments/`.

## 5. Run the Complete Mininet Demonstration

Use three separate terminals. Keep the project root as the working directory in each terminal.

### Terminal 1 — Start the SDN Controller

Activate the Python environment:

```bash
source sdn-env/bin/activate
```

Start the controller:

```bash
osken-manager controller/controller.py
```

The controller listens for OpenFlow switches on `127.0.0.1:6653`.

Keep this terminal running.

For static routing, use:

```bash
source sdn-env/bin/activate
ROUTING_MODE=static osken-manager controller/controller.py
```

Do not start both controller modes simultaneously.

### Terminal 2 — Start Mininet Topology

```bash
sudo python3 -m topology.topology
```

The topology creates:

- `h1` — Client (`10.0.0.1`)
- `h2` — Backup Server 1 (`10.0.0.2`)
- `h3` — Backup Server 2 (`10.0.0.3`)
- `s1`, `s2`, `s3`, `s4` — Open vSwitch switches

Check connectivity inside Mininet:

```text
pingall
h1 ping -c 3 10.0.0.2
h1 ping -c 3 10.0.0.3
```

### Start Both Backup Servers

Run these commands inside the Mininet CLI in Terminal 2:

```text
h2 python3 -m server.server 0.0.0.0 5000 backup/h2 &
h3 python3 -m server.server 0.0.0.0 5000 backup/h3 &
```

Both servers listen on TCP port `5000`.

### Send a File to Both Servers

Inside the Mininet CLI:

```text
h1 dd if=/dev/urandom of=/tmp/test_backup.dat bs=1M count=5

h1 python3 -m client.client /tmp/test_backup.dat 10.0.0.2:5000 10.0.0.3:5000
```

A successful run should report that both destinations stored and verified the file (for example, `2/2 backups verified`).

### Verify Stored Copies

```text
h2 sha256sum backup/h2/test_backup.dat
h3 sha256sum backup/h3/test_backup.dat
```

Compare both hashes with the source file hash. They should match if both copies are intact.

### Test Link Failure

Bring the Path A link between `s2` and `s4` down:

```text
link s2 s4 down
h1 ping -c 5 10.0.0.2
```

Restore the link:

```text
link s2 s4 up
```

Check the controller terminal for path and link-state messages.

### Stop Mininet and Clean Up

Exit Mininet:

```text
exit
```

Clean up stale Mininet interfaces:

```bash
sudo mn -c
```

Run this cleanup before starting another topology or experiment if Mininet reports interface errors such as `RTNETLINK answers: File exists`.

## 6. Run Automated Tests (Without Mininet)

The automated integration tests start local TCP server processes and do not require Mininet.

From the project root:

```bash
source sdn-env/bin/activate
python3 -m unittest discover -s tests -v
```

The current project test suite contains **7 discovered integration tests**, covering:

1. Two-replica backup and SHA-256 hash verification.
2. Detection and retransmission of corrupted chunks.
3. Resume after a connection drop.
4. Server termination and restart during backup.
5. Repair of a damaged stored chunk.
6. No-resume transfer behavior.
7. Reporting a failed destination while another destination succeeds.

The recorded project run completed all 7 discovered tests successfully. Results on other machines may vary depending on the Python version and environment.

## 7. Run Performance and Recovery Experiments

The experiment script creates and manages its own Mininet topology.

Exit any manually running Mininet CLI first, then clean up:

```bash
sudo mn -c
```

Activate the environment containing OS-Ken and Matplotlib:

```bash
source sdn-env/bin/activate
```

Run the experiments:

```bash
sudo env "PATH=$VIRTUAL_ENV/bin:$PATH" "$VIRTUAL_ENV/bin/python" -m experiments.measure --repeat 3
```

See available options:

```bash
python3 -m experiments.measure --help
```

The script supports options such as experiment duration, failure start time, failure duration, repeat count, routing modes, and disabling emulated delay.

Generate graphs:

```bash
python3 -m experiments.plot_results
```

Generated output is stored in:

```text
experiments/results/
```

It includes CSV measurement data, controller logs, `throughput.png`, and `recovery.png`.

## 8. Useful Manual Commands

Inside the Mininet CLI:

```text
nodes
net
links
dump
pingall
link s2 s4 down
link s2 s4 up
```

To run a local server/client test without Mininet, use the automated test suite in Section 6.

## 9. Important Notes and Limitations

- Run Python module commands from the repository root so imports resolve correctly.
- The Mininet topology and controller must agree on switch ports, link names, IP addresses, and OpenFlow settings.
- Dynamic routing depends on the controller receiving switch statistics and link-state events.
- Mininet experiments are simulations. Throughput and recovery time depend on CPU load, VM networking, and the host environment.
- The current automated suite has 7 discovered tests, not 24.
- Some Mininet test-plan cases were only partially demonstrated or blocked during evaluation. In particular, the separate congestion-rerouting scenario requiring additional background traffic was not completed.
- Refer to the submitted test report for the exact case-by-case status.
- Direct application-to-controller coordination through an API is not currently implemented.

## 10. Repository Structure

```text
project/
├── client/
│   ├── __init__.py
│   └── client.py
│
├── controller/
│   ├── __init__.py
│   └── controller.py
│
├── experiments/
│   ├── __init__.py
│   ├── measure.py
│   ├── plot_results.py
│   ├── traffic.py
│   └── results/
│
├── protocol/
│   ├── __init__.py
│   └── protocol.py
│
├── server/
│   ├── __init__.py
│   └── server.py
│
├── tests/
│   ├── __init__.py
│   ├── test_client.py
│   └── try_client.py
│
├── topology/
│   ├── __init__.py
│   └── topology.py
│
└── utils/
    ├── __init__.py
    └── checksum.py
```

## 11. Conclusion

This project demonstrates a distributed TCP-based file backup system integrated with Software-Defined Networking (SDN).

It divides files into chunks, transfers them to two backup servers, and verifies the stored copies using SHA-256 checksums. It also includes mechanisms for handling interrupted transfers, retransmitting corrupted chunks, and resuming transfers where supported.

The SDN controller manages forwarding and path selection across a Mininet network with alternative routes. The experiments evaluate throughput and recovery behavior under link failures in static and dynamic routing modes.

Together, the application-level backup protocol and SDN-controlled network provide a practical prototype for studying reliable, fault-tolerant file backup over programmable networks. The documented test results describe the implementation's current coverage and limitations.
