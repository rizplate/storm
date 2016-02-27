#!/usr/bin/env python
import os
import time
import logging
import colorlog
import threading
import ConfigParser
import concurrent.futures as futures

# from colors import colors
from progressbar import ProgressBar, Percentage, Bar, Timer, ETA
from contextlib import contextmanager
from fabric.state import output
from fabric.api import settings, lcd, task, local, abort, shell_env, env

import boto
from boto.exception import EC2ResponseError

from azure.servicemanagement import ServiceManagementService, ConfigurationSetInputEndpoint
from azure.common import AzureHttpError

log = logging.getLogger(__name__)

formatter = colorlog.ColoredFormatter(
    '%(log_color)s%(levelname)s%(reset)s [%(bold)s%(asctime)s%(reset)s] [%(blue)s%(name)s.%(funcName)s%(reset)s:%(bold)s%(lineno)d%(reset)s] %(message)s',
    datefmt="%H:%M:%S",
    reset=True,
    log_colors=colorlog.default_log_colors)

debug = logging.getLogger('debug')
debug.setLevel(logging.DEBUG)
debug.propagate = False
debuglog = logging.FileHandler('debug.log')
debuglog.setFormatter(formatter)
debug.addHandler(debuglog)

logging.getLogger("requests").setLevel(logging.WARNING)
logging.getLogger('urllib3').setLevel(logging.WARNING)
logging.getLogger('boto').setLevel(logging.CRITICAL)

class FabricException(Exception):
    pass
env.abort_exception = FabricException
# env.warn_only = True

# Get AWS credentials
try:
    path = os.path.join(os.path.expanduser("~"), ".storm", "aws")
    config = ConfigParser.ConfigParser()
    config.read([str(os.path.join(path, "credentials"))])
    AWS_ACCESS_KEY = config.get('Credentials', 'aws_access_key_id')
    AWS_SECRET_KEY = config.get('Credentials', 'aws_secret_access_key')
except:
    # logging.warn("No AWS credentials set. Please set them in ~/.aws/credentials")
    AWS_ACCESS_KEY = None
    AWS_SECRET_KEY = None

# Get Azure credentials
try:
    path = os.path.join(os.path.expanduser("~"), ".storm", "azure")
    subscription_id_path = os.path.join(path, "subscription-id")
    with open(subscription_id_path, 'r') as f:
        AZURE_SUBSCRIPTION_ID = f.read().splitlines()[0]
    AZURE_CERTIFICATE = os.path.join(path, "certificate.pem")
except Exception as e:
    # logging.warn("Unable to read Azure credentials in ~/.storm/azure: %s" % repr(e))
    AZURE_SUBSCRIPTION_ID = None
    AZURE_CERTIFICATE = None

# Get DigitalOcean credentials
try:
    path = os.path.join(os.path.expanduser("~"), ".storm", "digitalocean")
    with open(os.path.join(path, "token"), 'r') as f:
        DIGITALOCEAN_ACCESS_TOKEN = f.read().splitlines()[0]
except Exception as e:
    # logging.warn("Unable to read DigitalOcean credentials in ~/.storm/digitalocean: %s" % repr(e))
    DIGITALOCEAN_ACCESS_TOKEN = None

widgets = ['Progress: ', Percentage(), '   ', Timer(), ' ', Bar(marker='#', left='[', right=']'), ' ', ETA()]
completed = 0

ticker = None
def tick(progress):
    global ticker
    global completed
    progress.update(completed)
    ticker = threading.Timer(1.0, tick, args=[progress])
    ticker.start()

def set_logging(debug=False):
    if debug:
        logging.basicConfig(
            level=logging.DEBUG,
            format="[%(asctime)s] %(levelname)s [%(name)s.%(funcName)s:%(lineno)d] %(message)s",
            datefmt="%H:%M:%S")
    else:
        logging.basicConfig(
            level=logging.INFO,
            format="%(message)s",
            datefmt="%H:%M:%S")

        # Set Fabric' output level, defaults:
        # {'status': True, 'stdout': True, 'warnings': True, 'running': True,
        #  'user': True, 'stderr': True, 'aborts': True, 'debug': False}
        if not debug:
            output['aborts'] = False
            output['warnings'] = False
            output['running'] = False
            output['status'] = False

@contextmanager
def rollback(instances):
    try:
        yield
    except SystemExit:
        teardown(instances)
        abort("Bad failure...")

def machine_env(instance, swarm=False):
    env_ = {}
    log.debug("Getting environment for %s" % instance)
    env_export = machine("env --shell bash %s%s" % ("--swarm " if swarm else "", instance))
    log.debug("Environment: %s" % env_export)
    exports = env_export.splitlines()
    for export in exports:
        export = export[7:]  # remove "export "...
        if export.startswith("DOCKER_TLS_VERIFY"):
            log.debug(export)
            tls = export.split("=")[-1][1:-1]
        if export.startswith("DOCKER_CERT_PATH"):
            log.debug(export)
            cert_path = export.split("=")[-1][1:-1]
        if export.startswith("DOCKER_HOST"):
            log.debug(export)
            host = export.split("=")[-1][1:-1]
    if not tls or not cert_path or not host:
        log.debug(exports)
        return False
    env_['tls'] = tls
    env_['cert_path'] = cert_path
    env_['host'] = host
    return env_

def docker(cmd, capture=True):
    """
    Run Docker command
    """
    try:
        out = local("docker %s" % cmd, capture=capture)
        return out
    except FabricException as e:
        debug.debug("Exception running docker: %r" % e)

def machine(cmd, capture=True, progress=None):
    """
    Run Machine command
    """
    try:
        out = local("docker-machine %s" % cmd, capture=capture)
        if progress:
            global completed
            completed += 1
            progress.update(completed)
        return out
    except FabricException as e:
        debug.debug("Exception running docker-machine: %r" % e)

def compose(cmd, capture=True, progress=None):
    """
    Run Compose command
    """
    try:
        out = local("docker-compose %s" % cmd, capture=capture)
        if progress:
            global completed
            completed += 1
            progress.update(completed)
        return out
    except FabricException as e:
        debug.debug("Exception running docker-compose: %r" % e)

def machine_list():
    """
    List machines
    """
    return machine("ls", capture=True)

def active(instance):
    machine("active %s" % instance)

def pull(image):
    docker("pull %s" % image)

def build(folder, tag):
    docker("build -t %s %s" % (tag, folder))

def run(name, image, options, command, capture=True):
    out = docker("run --name %s %s %s %s" % (name, options, image, command), capture=capture)
    debug.debug("Started: %s" % out)

def stop(name, rm=True, capture=True):
    out = docker("stop --time=30 %s" % name, capture=capture)
    debug.debug("Stopped: %s" % out)
    if rm:
        out = docker("rm %s" % name, capture=capture)
        debug.debug("Removed: %s" % out)

def exec_(container, command):
    docker("exec -it %s %s", container, command)

def run_on(instance, image, options="", command="", name=None, progress=None):
    if name is None:
        name = instance
    env_ = machine_env(instance)
    if not env_:
        abort("Error getting machine environment")
    with shell_env(DOCKER_TLS_VERIFY=env_['tls'], DOCKER_CERT_PATH=env_['cert_path'], DOCKER_HOST=env_['host']):
        out = docker("run --name %s %s %s %s" % (name, options, image, command))
        debug.debug("Started on %s: %s" % (instance, out))
        if progress:
            global completed
            completed += 9
            progress.update(completed)

def stop_on(instance, capture=True, rm=True, progress=None):
    global completed
    env_ = machine_env(instance)
    if not env_:
        abort("Error getting machine environment")
    with shell_env(DOCKER_TLS_VERIFY=env_['tls'], DOCKER_CERT_PATH=env_['cert_path'], DOCKER_HOST=env_['host']):
        out = docker("stop --time=30 %s" % instance, capture=capture)
        debug.debug("Stopped on %s: %s" % (instance, out))
        if progress:
            completed += 9
            progress.update(completed)
    if rm:
        with shell_env(DOCKER_TLS_VERIFY=env_['tls'], DOCKER_CERT_PATH=env_['cert_path'], DOCKER_HOST=env_['host']):
            out = docker("rm -f %s" % instance, capture=capture)
            debug.debug("Removed on %s: %s" % (instance, out))
            if progress:
                completed += 9
                progress.update(completed)

def docker_on(instance, command, discovery=None, capture=True):
    env_ = machine_env(instance, swarm=True if discovery else False)
    if not env_:
        abort("Error getting machine environment")
    if discovery:
        with shell_env(DOCKER_TLS_VERIFY=env_['tls'], DOCKER_CERT_PATH=env_['cert_path'], DOCKER_HOST=env_['host'], DISCOVERY_IP=discovery):
            return docker(command, capture=capture)
    else:
        with shell_env(DOCKER_TLS_VERIFY=env_['tls'], DOCKER_CERT_PATH=env_['cert_path'], DOCKER_HOST=env_['host']):
            return docker(command, capture=capture)

def exec_on(instance, container, command):
    env_ = machine_env(instance)
    if not env_:
        abort("Error getting machine environment")
    with shell_env(DOCKER_TLS_VERIFY=env_['tls'], DOCKER_CERT_PATH=env_['cert_path'], DOCKER_HOST=env_['host']):
        exec_(container, command)

def pull_on(instance, image):
    env_ = machine_env(instance)
    if not env_:
        abort("Error getting machine environment")
    with shell_env(DOCKER_TLS_VERIFY=env_['tls'], DOCKER_CERT_PATH=env_['cert_path'], DOCKER_HOST=env_['host']):
        pull(image)

def build_on(instance, folder, tag):
    env_ = machine_env(instance)
    if not env_:
        abort("Error getting machine environment")
    with shell_env(DOCKER_TLS_VERIFY=env_['tls'], DOCKER_CERT_PATH=env_['cert_path'], DOCKER_HOST=env_['host']):
        build(folder, tag)

def compose_on(instance, command, discovery=None, capture=True):
    env_ = machine_env(instance, swarm=True if discovery else False)
    if not env_:
        abort("Error getting machine environment")
    if discovery:
        with shell_env(DOCKER_TLS_VERIFY=env_['tls'], DOCKER_CERT_PATH=env_['cert_path'], DOCKER_HOST=env_['host'], DISCOVERY_IP=discovery):
            out = compose(command, capture=capture)
    else:
        with shell_env(DOCKER_TLS_VERIFY=env_['tls'], DOCKER_CERT_PATH=env_['cert_path'], DOCKER_HOST=env_['host']):
            out = compose(command, capture=capture)
    if capture:
        debug.debug("Composed on %s: %s" % (instance, out))

def ssh_on(instance, command):
    out = machine("ssh %s -- %s" % (instance, command))
    return out

def scp_to(instance, src, dest):
    machine("scp %s %s:%s" % (src, instance, dest))

def create(instance, capture=True, progress=None):
    global completed

    # Delay instantiations slightly
    index = int(instance["name"].split("-")[2])
    time.sleep(index)

    if instance["provider"] == "aws":
        completed += 1
        progress.update(completed)

        create_aws(instance["name"],
                   vpc=instance["vpc"] if "vpc" in instance else None,
                   ami=instance["ami"] if "ami" in instance else None,
                   zone=instance["zone"] if "zone" in instance else "c",
                   region=instance["region"] if "region" in instance else "us-east-1",
                   instance_type=instance["size"] if "size" in instance else "t2.medium",
                   security_group=instance["security_group"] if "security_group" in instance else "docker-storm",
                   discovery=instance["discovery"] if "discovery" in instance else None,
                   capture=capture,
                   progress=progress)

    elif instance["provider"] == "azure":
        completed += 1
        progress.update(completed)

        create_azure(instance["name"],
                     size=instance["size"] if "size" in instance else "Small",
                     image=instance["image"] if "image" in instance else None,
                     location=instance["location"] if "location" in instance else "East US",
                     discovery=instance["discovery"] if "discovery" in instance else None,
                     capture=capture,
                     progress=progress)

    elif instance["provider"] == "digitalocean":
        completed += 1
        progress.update(completed)

        create_digitalocean(instance["name"],
                            size=instance["size"] if "size" in instance else "512mb",
                            image=instance["image"] if "image" in instance else None,
                            region=instance["region"] if "region" in instance else "nyc3",
                            discovery=instance["discovery"] if "discovery" in instance else None,
                            capture=capture,
                            progress=progress)

def create_aws(name, vpc=None, ami=None, region="us-east-1", zone="c", instance_type="t2.medium", security_group="docker-storm",
               discovery=None, capture=True, progress=None):
    """
    Launch an AWS instance
    """
    try:
        global completed

        swarm_options = (
            "--swarm --swarm-master "
            "--swarm-opt='replication=true' "
            # "--swarm-opt='advertise=eth0:3376' "
            "--swarm-discovery='consul://{0}:8500' "
            "--engine-opt='cluster-store=consul://{0}:8500' "
            "--engine-opt='cluster-advertise=eth0:2376' ".format(discovery)
        ) if discovery else ""

        conf = {
            "access_key": AWS_ACCESS_KEY,
            "secret_key": AWS_SECRET_KEY,
            "vpc": ("--amazonec2-vpc-id %s " % vpc) if vpc else "",
            "region": region,
            "zone": zone,
            "instance_type": instance_type,
            "security_group": security_group,
            "ami": ("--amazonec2-ami %s " % ami) if ami else "",
            "swarm": swarm_options,
            "name": name
        }
        out = local(("docker-machine create "
                     "--driver amazonec2 "
                     "--amazonec2-access-key {access_key} "
                     "--amazonec2-secret-key {secret_key} "
                     "{vpc}"
                     "--amazonec2-region {region} "
                     "--amazonec2-zone {zone} "
                     "--amazonec2-instance-type {instance_type} "
                     "--amazonec2-root-size 8 "
                     "--amazonec2-security-group {security_group} "
                     "{ami}"
                     "{swarm}"
                     "{name}").format(**conf),
                    capture=capture)

        if "Error" in out:
            debug.debug('Error creating %s, removing... The error was: %r' % (name, out))
            out = machine('rm -f %s' % name)
            debug.debug("Removed: %s" % out)

            if progress:
                completed += 9
                progress.update(completed)

        else:
            debug.debug("Launched %s: %s" % (name, out))

            if progress:
                completed += 7
                progress.update(completed)

            # Open overlay network ports in security group
            aws_security_group_ports(name, [{
                'protocol': 'udp',
                'from_port': '4789',
                'to_port': '4789'
            }, {
                'protocol': 'udp',
                'from_port': '7946',
                'to_port': '7946'
            }, {
                'protocol': 'tcp',
                'from_port': '7946',
                'to_port': '7946'
            }, {
                'protocol': 'tcp',  # TODO Separate service ports
                'from_port': '80',
                'to_port': '80'
            }, {
                'protocol': 'tcp',
                'from_port': '88',
                'to_port': '88'
            }, {
                'protocol': 'tcp',
                'from_port': '443',
                'to_port': '443'
            }, {
                'protocol': 'tcp',
                'from_port': '8545',
                'to_port': '8545'
            }], security_group)

            if progress:
                completed += 2
                progress.update(completed)

    except FabricException as e:
        debug.debug('Exception creating %s, removing... The error was: %r' % (name, e))
        out = machine('rm -f %s' % name)
        debug.debug("Removed: %s" % out)
        if progress:
            completed += 9
            progress.update(completed)

def create_azure(name, size="Small", location="East US", image=None,
                 discovery=None, capture=True, progress=None):
    """
    Launch an Azure instance
    """
    try:
        global completed

        swarm_options = (
            "--swarm --swarm-master "
            "--swarm-opt='replication=true' "
            # "--swarm-opt='advertise=eth0:3376' "
            "--swarm-discovery='consul://{0}:8500' "
            "--engine-opt='cluster-store=consul://{0}:8500' "
            "--engine-opt='cluster-advertise=eth0:2376' ".format(discovery)
        ) if discovery else ""

        conf = {
            "subscription_id": AZURE_SUBSCRIPTION_ID,
            "certificate": AZURE_CERTIFICATE,
            "size": size,
            "location": location,
            "image": ("--azure-image %s " % image) if image else "",
            "swarm": swarm_options,
            "name": name
        }
        out = local(("docker-machine create "
                     "--driver azure "
                     "--azure-subscription-id {subscription_id} "
                     "--azure-subscription-cert {certificate} "
                     "--azure-location '{location}' "
                     "--azure-size {size} "
                     "{image}"
                     "{swarm}"
                     "{name}").format(**conf),
                    capture=capture)

        if "Error" in out:
            debug.debug('Error creating %s, removing... The error was: %r' % (name, out))
            out = machine('rm -f %s' % name)
            debug.debug("Removed: %s" % out)
            if progress:
                completed += 9
                progress.update(completed)

        else:
            debug.debug("Launched %s: %s" % (name, out))

            if progress:
                completed += 7
                progress.update(completed)

            # Add endpoints for overlay network
            azure_add_endpoints(name, [{
                'service': 'docker vxlan',
                'protocol': 'udp',
                'port': '4789',
                'local_port': '4789'
            }, {
                'service': 'serf udp',
                'protocol': 'udp',
                'port': '7946',
                'local_port': '7946'
            }, {
                'service': 'serf tcp',
                'protocol': 'tcp',
                'port': '7946',
                'local_port': '7946'
            }, {
                'service': 'consul rpc',
                'protocol': 'tcp',
                'port': '8300',
                'local_port': '8300'
            }, {
                'service': 'consul lan',
                'protocol': 'tcp',
                'port': '8301',
                'local_port': '8301'
            }, {
                'service': 'consul lan udp',
                'protocol': 'udp',
                'port': '8301',
                'local_port': '8301'
            }, {
                'service': 'consul wan',
                'protocol': 'tcp',
                'port': '8302',
                'local_port': '8302'
            }, {
                'service': 'consul wan udp',
                'protocol': 'udp',
                'port': '8302',
                'local_port': '8302'
            }, {
                'service': 'consul',
                'protocol': 'tcp',
                'port': '8500',
                'local_port': '8500'
            }, {
                'service': 'http',  # TODO Separate service ports
                'protocol': 'tcp',
                'port': '80',
                'local_port': '80'
            }, {
                'service': 'haproxy stats',
                'protocol': 'tcp',
                'port': '88',
                'local_port': '88'
            }, {
                'service': 'https',
                'protocol': 'tcp',
                'port': '443',
                'local_port': '443'
            }, {
                'service': 'geth',
                'protocol': 'tcp',
                'port': '8545',
                'local_port': '8545'
            }])

            if progress:
                completed += 2
                progress.update(completed)

    except FabricException as e:
        debug.debug('Exception creating %s, removing... The error was: %r' % (name, e))
        out = machine('rm -f %s' % name)
        debug.debug("Removed: %s" % out)
        if progress:
            completed += 9
            progress.update(completed)

def create_digitalocean(name, size="512mb", region="nyc3", image=None,
                        discovery=None, capture=True, progress=None):
    """
    Launch a DigitalOcean instance
    """
    try:
        global completed

        swarm_options = (
            "--swarm --swarm-master "
            "--swarm-opt='replication=true' "
            # "--swarm-opt='advertise=eth0:3376' "
            "--swarm-discovery='consul://{0}:8500' "
            "--engine-opt='cluster-store=consul://{0}:8500' "
            "--engine-opt='cluster-advertise=eth0:2376' ".format(discovery)
        ) if discovery else ""

        conf = {
            "access_token": DIGITALOCEAN_ACCESS_TOKEN,
            "size": size,
            "region": region,
            "image": ("--digitalocean-image %s " % image) if image else "",
            "swarm": swarm_options,
            "name": name
        }
        out = local(("docker-machine create "
                     "--driver digitalocean "
                     "--digitalocean-access-token {access_token} "
                     "--digitalocean-region {region} "
                     "--digitalocean-size {size} "
                     "{image} "
                     "{swarm}"
                     "{name}").format(**conf),
                    capture=capture)

        if "Error" in out:
            debug.debug('Error creating %s, removing... The error was: %r' % (name, out))
            out = machine('rm -f %s' % name)
            debug.debug("Removed: %s" % out)
        else:
            debug.debug("Launched %s: %s" % (name, out))

            # TODO Open overlay network ports???

        if progress:
            completed += 9
            progress.update(completed)

    except FabricException as e:
        debug.debug('Exception creating %s, removing... The error was: %r' % (name, e))
        out = machine('rm -f %s' % name)
        debug.debug("Removed: %s" % out)
        if progress:
            completed += 9
            progress.update(completed)

def azure_add_endpoints(name, portConfigs):
    sms = ServiceManagementService(AZURE_SUBSCRIPTION_ID, AZURE_CERTIFICATE)
    role = sms.get_role(name, name, name)

    network_config = role.configuration_sets[0]
    for i, portConfig in enumerate(portConfigs):
        network_config.input_endpoints.input_endpoints.append(
            ConfigurationSetInputEndpoint(
                name=portConfig["service"],
                protocol=portConfig["protocol"],
                port=portConfig["port"],
                local_port=portConfig["local_port"],
                load_balanced_endpoint_set_name=None,
                enable_direct_server_return=True if portConfig["protocol"] == "udp" else False,
                idle_timeout_in_minutes=None if portConfig["protocol"] == "udp" else 4)
        )
    try:
        sms.update_role(name, name, name, network_config=network_config)
    except AzureHttpError as e:
        debug.debug("Exception opening ports for %s: %r" % (name, e))

def aws_security_group_ports(name, portConfigs, security_group="docker-storm"):
    ec2 = boto.connect_ec2(aws_access_key_id=AWS_ACCESS_KEY,
                           aws_secret_access_key=AWS_SECRET_KEY)

    group_id = None
    groups = ec2.get_all_security_groups()
    for group in groups:
        if group.name == security_group:
            group_id = group.id
            break

    if not group_id:
        raise ValueError("Could not find group ID for security group %s" % security_group)

    for i, portConfig in enumerate(portConfigs):
        try:
            ec2.authorize_security_group(group_id=group_id,
                                         ip_protocol=portConfig["protocol"],
                                         from_port=portConfig["from_port"],
                                         to_port=portConfig["to_port"],
                                         cidr_ip="0.0.0.0/0")
        except EC2ResponseError as e:
            debug.debug("Exception opening ports for %s: %r" % (name, e))

@task
def launch(instances):
    """
    Launch instances using create()
    """
    max_workers = len(instances)

    global completed
    completed = 0
    progress = ProgressBar(widgets=widgets, max_value=max_workers * 10).start()

    start = time.time()
    tick(progress)

    with futures.ThreadPoolExecutor(max_workers=12) as executor:
        future_node = dict((executor.submit(create,
                                            instances[instance],
                                            progress=progress), instance)
                           for instance in instances)

    for future in futures.as_completed(future_node, 300):
        instance = future_node[future]
        if future.exception() is not None:
            debug.debug('%s generated an exception: %r' % (instance, future.exception()))
        if future.result() and "Exception" not in future.result():
            debug.debug('Launched %s: %r' % (instance, future.result()))

    ticker.cancel()
    progress.finish()
    log.info("Launch duration: %ss" % (time.time() - start))

@task
def deploy_consul(instances, encrypt, path=None):
    # TODO SSL/TLS, custom image or path for compose file, ports/permissions for DigitalOcean?
    max_workers = len(instances)

    global completed
    completed = 0
    progress = ProgressBar(widgets=widgets, max_value=max_workers * 10).start()

    start = time.time()
    tick(progress)

    with futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_node = dict((executor.submit(compose_consul,
                                            instance,
                                            ip=instances[instance],
                                            servers=instances.values(),
                                            encrypt=encrypt,
                                            path=path,
                                            progress=progress), instance)
                           for instance in instances.keys())

    for future in futures.as_completed(future_node, 300):
        instance = future_node[future]
        if future.exception() is not None:
            debug.debug('%s generated an exception: %r' % (instance, future.exception()))
        if future.result() and "Exception" not in future.result():
            debug.debug('Launched %s: %r' % (instance, future.result()))

    ticker.cancel()
    progress.finish()
    log.info("Deploy Consul duration: %ss" % (time.time() - start))

def compose_consul(instance, ip, servers, encrypt, path=None, progress=None):
    # TODO SSL/TLS, custom image or path for compose file, ports/permissions for DigitalOcean?
    global completed
    if progress:
        completed += 1
        progress.update(completed)

    # with lcd(os.path.join(os.path.dirname(__file__), 'compose', 'discovery')):
    #     compose_on(instance, "up -d")

    #
    # Open ports
    #
    # FIXME Azure takes too long to add endpoints so we have to open ports all at once on launch...
    # if "-azure-" in instance:
    #     azure_add_endpoints(instance, [{
    #         'service': 'consul',
    #         'protocol': 'tcp',
    #         'port': '8500',
    #         'local_port': '8500'
    #     }])

    if "-aws-" in instance:
        aws_security_group_ports(instance, [{
            'protocol': 'tcp',
            'from_port': '8300',
            'to_port': '8300'
        }, {
            'protocol': 'tcp',
            'from_port': '8301',
            'to_port': '8301'
        }, {
            'protocol': 'udp',
            'from_port': '8301',
            'to_port': '8301'
        }, {
            'protocol': 'tcp',
            'from_port': '8302',
            'to_port': '8302'
        }, {
            'protocol': 'udp',
            'from_port': '8302',
            'to_port': '8302'
        }, {
            'protocol': 'tcp',
            'from_port': '8500',
            'to_port': '8500'
        }], 'docker-storm')

    if progress:
        completed += 1
        progress.update(completed)

    joins = ""
    # index = int(instance.split("-")[2])
    # if len(hosts) == 1 or index == 0:
    #     joins = " -bootstrap"
    for server in servers:
        if server != ip:
            joins += "-retry-join-wan='%s' " % server

    # Consul doesn't like our Azure hostnames, and docker-machine doesn't even
    # know the actual IP...
    if '-azure-' in instance:
        ip = local("getent hosts %s | awk '{ print $1 }'" % ip, capture=True)

    run_on(instance, "gliderlabs/consul-server:0.6", (
                     "-d "
                     "-p 8300:8300 "
                     "-p 8301:8301 -p 8301:8301/udp "
                     "-p 8302:8302 -p 8302:8302/udp "
                     "-p 8500:8500"),
           "-advertise-wan='%s' -bootstrap-expect=%d -dc='%s' -encrypt='%s' %s -rejoin" % (ip, len(servers), instance, encrypt, joins))
    # -advertise='%s'

    if progress:
        completed += 8
        progress.update(completed)

@task
def deploy_registrator(swarm_master, scale, discovery, path=None):
    # TODO custom path for compose files
    with lcd(os.path.join(os.path.dirname(__file__), 'compose', 'registrator')):
        # log.info("Deploying registrator from %s%s%s with discovery at %s%s%s" % (colors.GREEN,
        #                                                                             swarm_master,
        #                                                                             colors.ENDC,
        #                                                                             colors.HEADER,
        #                                                                             discovery,
        #                                                                             colors.ENDC))

        compose_on(swarm_master, "up -d", discovery, capture=False)
        compose_on(swarm_master, "scale registrator=%d" % scale, discovery, capture=False)

@task
def prepare_haproxy(instances, path=None):
    # TODO custom path?
    max_workers = len(instances)

    global completed
    completed = 0
    progress = ProgressBar(widgets=widgets, max_value=max_workers * 10).start()

    start = time.time()
    tick(progress)

    with futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_node = dict((executor.submit(prepare_haproxy_instance,
                                            instance,
                                            path=path,
                                            progress=progress), instance)
                           for instance in instances)

    for future in futures.as_completed(future_node, 300):
        instance = future_node[future]
        if future.exception() is not None:
            debug.debug('%s generated an exception: %r' % (instance, future.exception()))
        if future.result() and "Exception" not in future.result():
            debug.debug('Prepared %s: %r' % (instance, future.result()))

    ticker.cancel()
    progress.finish()
    log.info("Prepare HAProxy duration: %ss" % (time.time() - start))

def prepare_haproxy_instance(instance, path=None, progress=None):
    # Transfer SSL/TLS certificate for HAProxy endpoint
    global completed
    if progress:
        completed += 1
        progress.update(completed)

    certificate = os.path.join(os.path.expanduser("~"), ".storm", "certificate.pem")
    ssh_on(instance, "mkdir -p /home/ubuntu/.storm")

    if progress:
        completed += 4
        progress.update(completed)

    machine("scp %s %s:/home/ubuntu/.storm/" % (certificate, instance))

    if progress:
        completed += 5
        progress.update(completed)

@task
def deploy_haproxy(swarm_master, scale, discovery, path=None):
    # TODO custom path for compose files
    with lcd(os.path.join(os.path.dirname(__file__), 'compose', 'haproxy')):
        compose_on(swarm_master, "up -d", discovery)
        compose_on(swarm_master, "scale load-balancer=%d" % scale, discovery, capture=False)

@task
def stop_machines(machines):
    """
    Stop machines
    """
    max_workers = len(machines)

    global completed
    completed = 0
    progress = ProgressBar(widgets=widgets, max_value=max_workers * 10).start()

    start = time.time()
    tick(progress)

    with futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_node = dict((executor.submit(stop_machine,
                                            machine,
                                            progress=progress), machine)
                           for machine in machines)
        progress.update(max_workers)
        completed = max_workers

    for future in futures.as_completed(future_node, 30):
        instance = future_node[future]
        if future.exception() is not None:
            debug.debug("Exception stopping %s: %s" % (instance, future.exception()))
        if future.result() and "Exception" not in future.result():
            debug.debug("Stopped: %s" % future.result())

    ticker.cancel()
    progress.finish()
    log.info("Stop duration: %ss" % (time.time() - start))

def stop_machine(machine, progress=None):
    machine("stop %s" % machine)
    if progress:
        global completed
        completed += 9
        progress.update(completed)

@task
def cleanup(containers):
    """
    Generic cleanup routine for containers and images
    """
    with settings(warn_only=True):
        for container in containers:
            docker("stop --time=30 %s" % container)
            docker("rm $(docker ps -q -f status=exited)")
            docker("rmi $(docker images -f 'dangling=true' -q)")

@task
def teardown(instances):
    """
    Remove instances
    """
    max_workers = len(instances)

    global completed
    completed = 0
    progress = ProgressBar(widgets=widgets, max_value=max_workers).start()

    start = time.time()
    with futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_node = dict((executor.submit(machine,
                                            "rm -y %s" % instance,
                                            progress=progress), instance)
                           for instance in instances)

    for future in futures.as_completed(future_node, 30):
        instance = future_node[future]
        if future.exception() is not None:
            debug.debug('%s generated an exception: %r' % (instance, future.exception()))
        if future.result():
            debug.debug("Teardown: %s" % future.result())

    progress.finish()
    log.info("Teardown duration: %ss" % (time.time() - start))
