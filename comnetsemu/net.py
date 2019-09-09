"""
About: ComNetsEmu Network
"""

import os
import shutil
# import subprocess
import sys
# from shlex import split
from time import sleep

import docker
from comnetsemu.cli import spawnAttachedXterm
from comnetsemu.node import DockerContainer, DockerHost
from mininet.link import TCIntf
from mininet.log import debug, error, info
from mininet.net import Mininet
from mininet.node import Switch
from mininet.term import cleanUpScreens, makeTerms
from mininet.util import BaseString, checkRun

# ComNetsEmu version: should be consistent with README and LICENSE
VERSION = "0.1.4"

VNFMANGER_MOUNTED_DIR = "/tmp/comnetsemu/vnfmanager"


class Containernet(Mininet):
    """A Mininet sub-class with DockerHost related methods."""
    def __init__(self, **params):
        Mininet.__init__(self, **params)

    def addDockerHost(self, name, cls=DockerHost,
                      **params):  # pragma: no cover
        """Wrapper for addHost method that adds a Docker container as a host."""
        return self.addHost(name, cls=cls, **params)

    # MARK: Already add fix by patching mininet, keep it for un-updated setups
    #       Remove it when Mininet has a new release
    def addLink(self, node1, node2, port1=None, port2=None, cls=None,
                **params):  # pragma: no cover
        # MARK: Node1 should be the switch for DockerHost
        if isinstance(node1, DockerHost) and isinstance(node2, Switch):
            error("Switch should be the node1 to connect a DockerHost\n")
            self.stop()
            checkRun("ce -c")
            sys.exit(1)
        else:
            super(Containernet, self).addLink(node1, node2, port1, port2, cls,
                                              **params)

    def startTerms(self):  # pragma: no cover
        "Start a terminal for each node."
        if 'DISPLAY' not in os.environ:
            error("Error starting terms: Cannot connect to display\n")
            return
        info("*** Running terms on %s\n" % os.environ['DISPLAY'])
        cleanUpScreens()
        self.terms += makeTerms(self.controllers, 'controller')
        self.terms += makeTerms(self.switches, 'switch')
        dhosts = [h for h in self.hosts if isinstance(h, DockerHost)]
        for d in dhosts:
            self.terms.append(spawnAttachedXterm(d.name))
        rest = [h for h in self.hosts if h not in dhosts]
        self.terms += makeTerms(rest, 'host')

    def addLinkNamedIfce(self, src, dst, *args, **kwargs):  # pragma: no cover
        """Add a link with two named interfaces.
           - Name of interface 1: src-dst
           - Name of interface 2: dst-src
        """
        # Accept node objects or names
        src = src if not isinstance(src, BaseString) else self[src]
        dst = dst if not isinstance(dst, BaseString) else self[dst]
        self.addLink(src,
                     dst,
                     intfName1="-".join((src.name, dst.name)),
                     intfName2="-".join((dst.name, src.name)),
                     *args,
                     **kwargs)

    def ChangeHostIfceLoss(self, host, ifce, loss,
                           parent=" parent 5:1"):  # pragma: no cover
        """Change the loss rate of a TC interface.

        :param host (str,node): Name of the host
        :param ifce (str): Name of the TC interface
        :param loss: Loss rate in %
        :param parent (str): Handle of parent tc qdisc. Mininet uses 5:1 in a HTB
        """
        if isinstance(host, BaseString):
            host = self.get(host)
        if not host:
            error("Can not find the running host\n")
            return False
        tc_ifce = host.intf(ifce)
        if not isinstance(tc_ifce, TCIntf):
            error("The interface must be a instance of TCIntf\n")
            return False

        # WARN: The parent number is defined in mininet/link.py
        ret = host.cmd(
            "tc qdisc change dev {} {} handle 10: netem loss {}%".format(
                ifce, parent, loss))
        if ret != "":
            error("Failed to change loss. Error:%s\n", ret)
            return False

        return True

    def stop(self):
        super(Containernet, self).stop()


# MARK(Zuo): Maybe "AppContainerManager" is a better name.
#            So we have DockerHost emulating physicall machines and AppContainer
#            for applications. So Docker-In-Docker. Naming is difficult...
class VNFManager(object):
    """Manager for VNFs deployed on Mininet hosts (Docker-in-Docker)

    - To make is simple. It uses docker-py APIs to manage internal containers
      from host system.

    - It should communicate with SDN controller to manage internal containers
      adaptively.

    - Internal methods (starts with an underscore) should be documented after
      tests and before stable releases.

    Ref:
        [1] https://docker-py.readthedocs.io/en/stable/containers.html
    """

    docker_args_default = {
        "tty": True,  # -t
        "detach": True,  # -d
        # Used for cleanups
        "labels": {
            "comnetsemu": "dockercontainer"
        },
        # Required for CRIU checkpoint
        "security_opt": ["seccomp:unconfined"],
        # Shared directory in host OS
        "volumes": {
            VNFMANGER_MOUNTED_DIR: {
                'bind': VNFMANGER_MOUNTED_DIR,
                'mode': 'rw'
            }
        }
    }

    # Default delay between tries for Docker API
    retry_delay_secs = 0.1

    def __init__(self, net: Mininet):
        """Init the VNFManager

        :param net (Mininet): The mininet object, used to manage hosts via
        Mininet's API.
        """
        self.net = net
        self.dclt = docker.from_env()

        self._container_queue = list()
        # Fast search for added containers.
        self._name_container_map = dict()

        os.makedirs(VNFMANGER_MOUNTED_DIR, exist_ok=True)

    def _createContainer(self, name, dhost, dimage, dcmd, docker_args):
        docker_args_used = dict()
        if docker_args:
            docker_args_used.update(docker_args)
        # Override the essential parameters
        docker_args_used.update(self.docker_args_default)
        docker_args_used["name"] = name
        docker_args_used["image"] = dimage
        docker_args_used["cgroup_parent"] = "/docker/{}".format(dhost.did)
        docker_args_used["command"] = dcmd
        docker_args_used["network_mode"] = "container:{}".format(dhost.did)

        ret = self.dclt.containers.create(**docker_args_used)
        return ret

    def _waitContainerStart(self, name):  # pragma: no cover
        """Wait for container to start up running"""
        while not self._getDockerIns(name):
            debug("Failed to get container:%s" % (name))
            sleep(self.retry_delay_secs)
        dins = self._getDockerIns(name)

        while not dins.attrs["State"]["Running"]:
            sleep(self.retry_delay_secs)
            dins.reload()  # refresh information in 'attrs'

    def _waitContainerRemoved(self, name):  # pragma: no cover
        """Wait for container to be removed"""
        while self._getDockerIns(name):
            sleep(self.retry_delay_secs)

    def _getDockerIns(self, name):
        """Get the DockerContainer instance by name.

        :param name (str): Name of the container
        """
        try:
            dins = self.dclt.containers.get(name)
        except docker.errors.NotFound:
            return None
        return dins

    def getContainers(self, dhost: str) -> list:
        """Get containers deployed on the given DockerHost.

        :param dhost: Name of the DockerHost
        :return: A list of DockerContainer instances on given DockerHost
        """
        return [c for c in self._container_queue if c.dhost == dhost]

    def addContainer(self,
                     name: str,
                     dhost: str,
                     dimage: str,
                     dcmd: str,
                     wait: bool = True,
                     docker_args: dict = None) -> DockerContainer:
        """Create and run a new container inside a Mininet DockerHost.

        The manager retries with retry_cnt times to create the container if the
        dhost can not be found via docker-py API, but can be found in the
        Mininet host list. This happens during e.g. updating the CPU limitation
        of a running DockerHost instance.

        :param name (str): Name of the container
        :param dhost (str): Name of the host used for deployment
        :param dimage (str): Name of the docker image
        :param dcmd (str): Command to run after the creation
        :param wait (Bool): Wait until the container has the running state if True.
        :param docker_args (dict): All other keyword arguments supported by
        Docker-py.  e.g. CPU and memory related limitations. Some parameters are
        overriden for VNFManager's functionalities.

        Check cls.docker_args_default.

        :return (DockerContainer): Added DockerContainer instance
        :raise KeyError: dhost is not found in the network
        """

        dhost = self.net.get(dhost)
        dins = self._createContainer(name, dhost, dimage, dcmd, docker_args)
        dins.start()
        if wait:
            self._waitContainerStart(name)
        container = DockerContainer(name, dhost.name, dimage, dins)
        self._container_queue.append(container)
        self._name_container_map[container.name] = container
        return container

    def removeContainer(self, container: str, wait: bool = True) -> bool:
        """Remove the given internal container.

        :param container (str): Name of the to be removed container
        :param wait (Bool): Wait until the container is fully removed if True.

        :return (bool): Return True/False for success/fail remove.
        :raise ValueError: container is not found
        """

        container = self._name_container_map.get(container, None)
        if not container:
            raise ValueError("Can not find container with name: {container}")

        self._container_queue.remove(container)
        container.dins.remove(force=True)
        if wait:
            self._waitContainerRemoved(container.name)
        del self._name_container_map[container.name]
        return True

    @staticmethod
    def _calculate_cpu_percent(stats):
        """Calculate the CPU usage in percent with given stats JSON data"""
        cpu_count = len(stats["cpu_stats"]["cpu_usage"]["percpu_usage"])
        cpu_percent = 0.0
        cpu_delta = float(stats["cpu_stats"]["cpu_usage"]["total_usage"]) - \
            float(stats["precpu_stats"]["cpu_usage"]["total_usage"])
        system_delta = float(stats["cpu_stats"]["system_cpu_usage"]) - \
            float(stats["precpu_stats"]["system_cpu_usage"])
        if system_delta > 0.0:
            cpu_percent = cpu_delta / system_delta * 100.0 * cpu_count

        if cpu_percent > 100:  # pragma: no cover
            cpu_percent = 100

        return cpu_percent

    def monResourceStats(self,
                         container: str,
                         sample_num: int = 3,
                         sample_period: float = 1.0) -> list:
        """Monitor the resource stats of a container within a given time

        :param container (name): Name of the container
        :param sample_num (int): Number of samples
        :param sample_period (float): Sleep period for each sample

        :return (list): A list of resource usages. Each item is a tuple
        (cpu_usg, mem_usg)
        :raise ValueError: container is not found
        """

        container = self._name_container_map.get(container, None)
        if not container:
            raise ValueError(f"Can not found container with name: {container}")

        n = 0
        usages = list()
        while n < sample_num:
            stats = container.getCurrentStats()
            mem_stats = stats["memory_stats"]
            mem_usg = mem_stats["usage"] / (1024**2)
            cpu_usg = self._calculate_cpu_percent(stats)
            usages.append((cpu_usg, mem_usg))
            sleep(sample_period)
            n += 1

        return usages

    # BUG: Checkpoint inside container breaks the networking of outside
    # container if container networking mode is used.
    # def checkpoint(self, container: str) -> str:
    #     container = self._name_container_map.get(container, None)
    #     if not container:
    #         raise ValueError(f"Can not found container with name: {container}")
    #     ckpath = os.path.join(VNFMANGER_MOUNTED_DIR, f"{container.name}")
    #     # MARK: Docker-py does not provide API for checkpoint and restore,
    #     # Docker CLI is directly used with subprocess as a temp workaround.
    #     subprocess.run(split(
    #         f"docker checkpoint create --checkpoint-dir={ckpath} {container.name} {container.name}"
    #     ),
    #                    check=True,
    #                    stdout=subprocess.DEVNULL,
    #                    stderr=subprocess.DEVNULL)

    #     return ckpath

    def stop(self):
        debug("STOP: {} containers in the VNF queue: {}\n".format(
            len(self._container_queue), ", ".join(
                (c.name for c in self._container_queue))))

        # Avoid missing delete internal containers manually before stop
        for c in self._container_queue:
            c.terminate()
            c.dins.remove(force=True)

        self.dclt.close()
        shutil.rmtree(VNFMANGER_MOUNTED_DIR)
