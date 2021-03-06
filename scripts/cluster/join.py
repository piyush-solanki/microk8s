#!/usr/bin/python3
import base64
import random
import string
import subprocess
import os
import getopt
import sys
import time

import requests
import socket
import shutil
import urllib3
import yaml

from common.utils import try_set_file_permissions, is_node_running_dqlite

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
CLUSTER_API = "cluster/api/v1.0"
snapdata_path = os.environ.get('SNAP_DATA')
snap_path = os.environ.get('SNAP')
ca_cert_file_via_env = "${SNAP_DATA}/certs/ca.remote.crt"
ca_cert_file = "{}/certs/ca.remote.crt".format(snapdata_path)
callback_token_file = "{}/credentials/callback-token.txt".format(snapdata_path)
callback_tokens_file = "{}/credentials/callback-tokens.txt".format(snapdata_path)
server_cert_file_via_env = "${SNAP_DATA}/certs/server.remote.crt"
server_cert_file = "{}/certs/server.remote.crt".format(snapdata_path)

cluster_dir = "{}/var/kubernetes/backend".format(snapdata_path)
cluster_backup_dir = "{}/var/kubernetes/backend.backup".format(snapdata_path)
cluster_cert_file = "{}/cluster.crt".format(cluster_dir)
cluster_key_file = "{}/cluster.key".format(cluster_dir)


def get_connection_info(master_ip, master_port, token, callback_token=None, cluster_type="etcd"):
    """
    Contact the master and get all connection information

    :param master_ip: the master IP
    :param master_port: the master port
    :param token: the token to contact the master with
    :param cluster_type: the type of cluster we want to join, etcd or dqlite

    :return: the json response of the master
    """
    cluster_agent_port = "25000"
    filename = "{}/args/cluster-agent".format(snapdata_path)
    with open(filename) as fp:
        for _, line in enumerate(fp):
            if line.startswith("--port"):
                port_parse = line.split(' ')
                port_parse = port_parse[-1].split('=')
                cluster_agent_port = port_parse[0].rstrip()

    if cluster_type == "dqlite":
        req_data = {"token": token,
                    "hostname": socket.gethostname(),
                    "port": cluster_agent_port}

        # TODO: enable ssl verification
        connection_info = requests.post("https://{}:{}/{}/join".format(master_ip, master_port, CLUSTER_API_V2),
                                        json=req_data,
                                        verify=False)  # type: requests.models.Response
    else:
        req_data = {"token": token,
                    "hostname": socket.gethostname(),
                    "port": cluster_agent_port,
                    "callback": callback_token}

        # TODO: enable ssl verification
        connection_info = requests.post("https://{}:{}/{}/join".format(master_ip, master_port, CLUSTER_API),
                                        json=req_data,
                                        verify=False)

    if connection_info.status_code != 200:
        message = "Error code {}.".format(connection_info.status_code)  # type: str
        if connection_info.headers.get('content-type') == 'application/json':
            res_data = connection_info.json()  # type: Dict[str, str]
            if 'error' in res_data:
                message = "{} {}".format(message, res_data["error"])
        print("Failed to join cluster. {}".format(message))
        exit(1)
    return connection_info.json()


def usage():
    print("Join a cluster: microk8s join <master>:<port>/<token>")


def set_arg(key, value, file):
    """
    Set an arguement to a file

    :param key: argument name
    :param value: value
    :param file: the arguments file
    """
    filename = "{}/args/{}".format(snapdata_path, file)
    filename_remote = "{}/args/{}.remote".format(snapdata_path, file)
    done = False
    with open(filename_remote, 'w+') as back_fp:
        with open(filename, 'r+') as fp:
            for _, line in enumerate(fp):
                if line.startswith(key):
                    done = True
                    if value is not None:
                        back_fp.write("{} {}\n".format(key, value))
                else:
                    back_fp.write("{}".format(line))
        if not done and value is not None:
            back_fp.write("{} {}\n".format(key, value))

    shutil.copyfile(filename, "{}.backup".format(filename))
    try_set_file_permissions("{}.backup".format(filename))
    shutil.copyfile(filename_remote, filename)
    try_set_file_permissions(filename)
    os.remove(filename_remote)


def get_etcd_client_cert(master_ip, master_port, token):
    """
    Get a signed cert to access etcd

    :param master_ip: master ip
    :param master_port: master port
    :param token: token to contact the master with
    """
    cer_req_file = "{}/certs/server.remote.csr".format(snapdata_path)
    cmd_cert = "openssl req -new -sha256 -key {SNAP_DATA}/certs/server.key -out {csr} " \
               "-config {SNAP_DATA}/certs/csr.conf".format(SNAP_DATA=snapdata_path, csr=cer_req_file)
    subprocess.check_call(cmd_cert.split())
    with open(cer_req_file) as fp:
        csr = fp.read()
        req_data = {'token': token, 'request': csr}
        # TODO: enable ssl verification
        signed = requests.post("https://{}:{}/{}/sign-cert".format(master_ip, master_port, CLUSTER_API),
                               json=req_data,
                               verify=False)
        if signed.status_code != 200:
            print("Failed to sign certificate. {}".format(signed.json()["error"]))
            exit(1)
        info = signed.json()
        with open(server_cert_file, "w") as cert_fp:
            cert_fp.write(info["certificate"])
        try_set_file_permissions(server_cert_file)


def update_flannel(etcd, master_ip, master_port, token):
    """
    Configure flannel

    :param etcd: etcd endpoint
    :param master_ip: master ip
    :param master_port: master port
    :param token: token to contact the master with
    """
    get_etcd_client_cert(master_ip, master_port, token)
    etcd = etcd.replace("0.0.0.0", master_ip)
    set_arg("--etcd-endpoints", etcd, "flanneld")
    set_arg("--etcd-cafile", ca_cert_file_via_env, "flanneld")
    set_arg("--etcd-certfile", server_cert_file_via_env, "flanneld")
    set_arg("--etcd-keyfile", "${SNAP_DATA}/certs/server.key", "flanneld")

    subprocess.check_call("systemctl restart snap.microk8s.daemon-flanneld.service".split())


def ca_one_line(ca):
    """
    The CA in one line
    :param ca: the ca
    :return: one line
    """
    return base64.b64encode(ca.encode('utf-8')).decode('utf-8')


def create_kubeconfig(token, ca, master_ip, api_port, filename, user):
    """
    Create a kubeconfig file. The file in stored under credentials named after the user

    :param token: the token to be in the kubeconfig
    :param ca: the ca
    :param master_ip: the master node IP
    :param api_port: the API server port
    :param filename: the name of the config file
    :param user: the user to use al login
    """
    snap_path = os.environ.get('SNAP')
    config_template = "{}/microk8s-resources/{}".format(snap_path, "kubelet.config.template")
    config = "{}/credentials/{}".format(snapdata_path, filename)
    shutil.copyfile(config, "{}.backup".format(config))
    try_set_file_permissions("{}.backup".format(config))
    ca_line = ca_one_line(ca)
    with open(config_template, 'r') as tfp:
        with open(config, 'w+') as fp:
            config_txt = tfp.read()
            config_txt = config_txt.replace("CADATA", ca_line)
            config_txt = config_txt.replace("NAME", user)
            config_txt = config_txt.replace("TOKEN", token)
            config_txt = config_txt.replace("127.0.0.1", master_ip)
            config_txt = config_txt.replace("16443", api_port)
            fp.write(config_txt)
        try_set_file_permissions(config)


def update_kubeproxy(token, ca, master_ip, api_port, hostname_override):
    """
    Configure the kube-proxy

    :param token: the token to be in the kubeconfig
    :param ca: the ca
    :param master_ip: the master node IP
    :param api_port: the API server port
    :param hostname_override: the hostname override in case the hostname is not resolvable
    """
    create_kubeconfig(token, ca, master_ip, api_port, "proxy.config", "kubeproxy")
    set_arg("--master", None, "kube-proxy")
    if hostname_override:
        set_arg("--hostname-override", hostname_override, "kube-proxy")

    subprocess.check_call("systemctl restart snap.microk8s.daemon-proxy.service".split())


def update_kubelet(token, ca, master_ip, api_port):
    """
    Configure the kubelet

    :param token: the token to be in the kubeconfig
    :param ca: the ca
    :param master_ip: the master node IP
    :param api_port: the API server port
    """
    create_kubeconfig(token, ca, master_ip, api_port, "kubelet.config", "kubelet")
    set_arg("--client-ca-file", "${SNAP_DATA}/certs/ca.remote.crt", "kubelet")
    subprocess.check_call("systemctl restart snap.microk8s.daemon-kubelet.service".split())


def store_remote_ca(ca):
    """
    Store the remote ca

    :param ca: the CA
    """
    with open(ca_cert_file, 'w+') as fp:
        fp.write(ca)
    try_set_file_permissions(ca_cert_file)


def mark_cluster_node():
    """
    Mark a node as being part of a cluster by creating a var/lock/clustered.lock
    """
    lock_file = "{}/var/lock/clustered.lock".format(snapdata_path)
    open(lock_file, 'a').close()
    os.chmod(lock_file, 0o700)
    services = ['etcd', 'apiserver', 'apiserver-kicker', 'controller-manager', 'scheduler']
    for service in services:
        subprocess.check_call("systemctl restart snap.microk8s.daemon-{}.service".format(service).split())


def generate_callback_token():
    """
    Generate a token and store it in the callback token file

    :return: the token
    """
    token = ''.join(random.choice(string.ascii_uppercase + string.digits) for _ in range(64))
    with open(callback_token_file, "w") as fp:
        fp.write("{}\n".format(token))

    try_set_file_permissions(callback_token_file)
    return token


def store_base_kubelet_args(args_string):
    """
    Create a kubelet args file from the set of args provided

    :param args_string: the arguments provided
    """
    args_file = "{}/args/kubelet".format(snapdata_path)
    with open(args_file, "w") as fp:
        fp.write(args_string)
    try_set_file_permissions(args_file)


def reset_current_installation():
    """
    Take a node out of a cluster
    """
    lock_file = "{}/var/lock/clustered.lock".format(snapdata_path)
    if not os.path.isfile(lock_file):
        print("Not in clustering mode.")
        exit(2)

    os.remove(lock_file)
    os.remove(ca_cert_file)
    os.remove(callback_token_file)
    os.remove(server_cert_file)

    for config_file in ["kubelet", "flanneld", "kube-proxy"]:
        shutil.copyfile("{}/default-args/{}".format(snap_path, config_file),
                        "{}/args/{}".format(snapdata_path, config_file))

    for user in ["proxy", "kubelet"]:
        config = "{}/credentials/{}.config".format(snapdata_path, user)
        shutil.copyfile("{}.backup".format(config), config)

    subprocess.check_call("{}/microk8s-stop.wrapper".format(snap_path).split())
    waits = 10
    while waits > 0:
        try:
            subprocess.check_call("{}/microk8s-start.wrapper".format(snap_path).split())
            break
        except subprocess.CalledProcessError:
            print("Services not ready to start. Waiting...")
            time.sleep(5)
            waits -= 1


def remove_kubelet_token(node):
    """
    Remove a token for a node in the known tokens

    :param node: the name of the node
    """
    file = "{}/credentials/known_tokens.csv".format(snapdata_path)
    backup_file = "{}.backup".format(file)
    token = "system:node:{}".format(node)
    # That is a critical section. We need to protect it.
    with open(backup_file, 'w') as back_fp:
        with open(file, 'r') as fp:
            for _, line in enumerate(fp):
                if token in line:
                    continue
                back_fp.write("{}".format(line))

    try_set_file_permissions(backup_file)
    shutil.copyfile(backup_file, file)


def remove_callback_token(node):
    """
    Remove a callback token

    :param node: the node
    """
    tmp_file = "{}.tmp".format(callback_tokens_file)
    if not os.path.isfile(callback_tokens_file):
        open(callback_tokens_file, 'a+')
        os.chmod(callback_tokens_file, 0o600)
    with open(tmp_file, "w") as backup_fp:
        os.chmod(tmp_file, 0o600)
        with open(callback_tokens_file, 'r+') as callback_fp:
            for _, line in enumerate(callback_fp):
                parts = line.split()
                if parts[0] == node:
                    continue
                else:
                    backup_fp.write(line)

    try_set_file_permissions(tmp_file)
    shutil.move(tmp_file, callback_tokens_file)


def remove_node(node):
    try:
        # Make sure this node exists
        subprocess.check_call("{}/microk8s-kubectl.wrapper get no {}".format(snap_path, node).split(),
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        print("Node {} does not exist.".format(node))
        exit(1)

    remove_kubelet_token(node)
    remove_callback_token(node)
    subprocess.check_call("{}/microk8s-kubectl.wrapper delete no {}".format(snap_path, node).split(),
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def get_token(name, tokens_file = "known_tokens.csv"):
    """
    Get token from known_tokens file

    :param name: the name of the node
    :param tokens_file: the file where the tokens should go
    :returns: the token or None(if name doesn't exist)
    """
    file = "{}/credentials/{}".format(snapdata_path, tokens_file)
    with open(file) as fp:
        for line in fp:
            if name in line:
                parts = line.split(',')
                return parts[0].rstrip()
    return None


def store_cert(filename, payload):
    """
    Store a certificate

    :param filename: where to store the certificate
    :param payload: certificate payload
    """
    file_with_path = "{}/certs/{}".format(snapdata_path, filename)
    backup_file_with_path = "{}.backup".format(file_with_path)
    shutil.copyfile(file_with_path, backup_file_with_path)
    try_set_file_permissions(backup_file_with_path)
    with open(file_with_path, 'w+') as fp:
        fp.write(payload)
    try_set_file_permissions(file_with_path)


def store_cluster_certs(cluster_cert, cluster_key):
    """
    Store the dqlite cluster certs

    :param cluster_cert: the cluster certificate
    :param cluster_key: the cluster certificate key
    """
    with open(cluster_cert_file, 'w+') as fp:
        fp.write(cluster_cert)
    try_set_file_permissions(cluster_cert_file)
    with open(cluster_key_file, 'w+') as fp:
        fp.write(cluster_key)
    try_set_file_permissions(cluster_key_file)


def create_admin_kubeconfig(ca):
    """
    Create a kubeconfig file. The file in stored under credentials named after the admin

    :param ca: the ca
    """
    token = get_token("admin", "basic_auth.csv")
    if not token:
        print("Error, could not locate admin token. Joining cluster failed.")
        exit(2)
    assert token is not None
    config_template = "{}/microk8s-resources/{}".format(snap_path, "client.config.template")
    config = "{}/credentials/client.config".format(snapdata_path)
    shutil.copyfile(config, "{}.backup".format(config))
    try_set_file_permissions("{}.backup".format(config))
    ca_line = ca_one_line(ca)
    with open(config_template, 'r') as tfp:
        with open(config, 'w+') as fp:
            config_txt = tfp.read()
            config_txt = config_txt.replace("CADATA", ca_line)
            config_txt = config_txt.replace("NAME", "admin")
            config_txt = config_txt.replace("AUTHTYPE", "password")
            config_txt = config_txt.replace("PASSWORD", token)
            fp.write(config_txt)
        try_set_file_permissions(config)


def store_callback_token(token):
    """
    Store the callback token

    :param stoken: the callback token
    """
    callback_token_file = "{}/credentials/callback-token.txt".format(snapdata_path)
    with open(callback_token_file, "w") as fp:
        fp.write(token)
    try_set_file_permissions(callback_token_file)


def restart_all_services():
    """
    Restart all services
    """
    subprocess.check_call("{}/microk8s-stop.wrapper".format(snap_path).split())
    waits = 10
    while waits > 0:
        try:
            subprocess.check_call("{}/microk8s-start.wrapper".format(snap_path).split())
            break
        except subprocess.CalledProcessError:
            time.sleep(5)
            waits -= 1


def update_dqlite(cluster_cert, cluster_key, voters, host):
    """
    Configure the dqlite cluster

    :param cluster_cert: the dqlite cluster cert
    :param cluster_key: the dqlite cluster key
    :param voters: the dqlite voters
    :param host: the hostname others see of this node
    """
    subprocess.check_call("systemctl stop snap.microk8s.daemon-apiserver.service".split())
    time.sleep(10)
    shutil.rmtree(cluster_backup_dir, ignore_errors=True)
    shutil.move(cluster_dir, cluster_backup_dir)
    os.mkdir(cluster_dir)
    store_cluster_certs(cluster_cert, cluster_key)
    with open("{}/info.yaml".format(cluster_backup_dir)) as f:
        data = yaml.load(f, Loader=yaml.FullLoader)

    # TODO make port configurable
    init_data = {'Cluster': voters, 'Address': "{}:19001".format(host)}
    with open("{}/init.yaml".format(cluster_dir), 'w') as f:
        yaml.dump(init_data, f)

    subprocess.check_call("systemctl start snap.microk8s.daemon-apiserver.service".split())

    waits = 10
    print("Waiting for node to join the cluster.", end=" ", flush=True)
    while waits > 0:
        try:
            out = subprocess.check_output("curl https://{}/cluster/ --cacert {} --key {} --cert {} -k -s"
                                    .format(data['Address'], cluster_cert_file, cluster_key_file,
                                            cluster_cert_file).split());
            if data['Address'] in out.decode():
                break
            else:
                print(".", end=" ", flush=True)
                time.sleep(5)
                waits -= 1

        except subprocess.CalledProcessError:
            print("..", end=" ", flush=True)
            time.sleep(5)
            waits -= 1
    print(" ")
    restart_all_services()


def join_dqlite(connection_parts):
    """
    Configure node to join a dqlite cluster.

    :param connection_parts: connection string parts
    """
    token = connection_parts[1]
    master_ep = connection_parts[0].split(":")
    master_ip = master_ep[0]
    master_port = master_ep[1]
    info = get_connection_info(master_ip, master_port, token, cluster_type="dqlite")

    hostname_override = info['hostname_override']

    store_cert("ca.crt", info["ca"])
    store_cert("ca.key", info["ca_key"])
    store_cert("server.crt", info["server_cert"])
    store_cert("server.key", info["server_cert_key"])
    store_cert("serviceaccount.key", info["service_account_key"])
    store_cert("front-proxy-client.crt", info["proxy_cert"])
    store_cert("front-proxy-client.key", info["proxy_cert_key"])
    # triplets of [username in known_tokens.csv, username in kubeconfig, kubeconfig filename name]
    for component in [("kube-proxy", "kubeproxy", "proxy.config"),
                      ("kubelet", "kubelet", "kubelet.config"),
                      ("kube-controller-manager", "controller", "controller.config"),
                      ("kube-scheduler", "scheduler", "scheduler.config")]:
        component_token = get_token(component[0])
        if not component_token:
            print("Error, could not locate {} token. Joining cluster failed.".format(component[0]))
            exit(3)
        assert token is not None
        # TODO make this configurable
        create_kubeconfig(component_token, info["ca"], "127.0.0.1", "16443", component[2], component[1])
    create_admin_kubeconfig(info["ca"])
    store_base_kubelet_args(info["kubelet_args"])
    store_callback_token(info["callback_token"])

    update_dqlite(info["cluster_cert"], info["cluster_key"], info["voters"], hostname_override)


def join_etcd(connection_parts):
    """
    Configure node to join an etcd cluster.

    :param connection_parts: connection string parts
    """
    token = connection_parts[1]
    master_ep = connection_parts[0].split(":")
    master_ip = master_ep[0]
    master_port = master_ep[1]
    callback_token = generate_callback_token()
    info = get_connection_info(master_ip, master_port, token, callback_token=callback_token)
    store_base_kubelet_args(info["kubelet_args"])
    hostname_override = None
    if 'hostname_override' in info:
        hostname_override = info['hostname_override']
    store_remote_ca(info["ca"])
    update_flannel(info["etcd"], master_ip, master_port, token)
    update_kubeproxy(info["kubeproxy"], info["ca"], master_ip, info["apiport"], hostname_override)
    update_kubelet(info["kubelet"], info["ca"], master_ip, info["apiport"])
    mark_cluster_node()


if __name__ == "__main__":
    try:
        opts, args = getopt.gnu_getopt(sys.argv[1:], "h", ["help"])
    except getopt.GetoptError as err:
        print(err)  # will print something like "option -a not recognized"
        usage()
        sys.exit(2)
    for o, a in opts:
        if o in ("-h", "--help"):
            usage()
            sys.exit(1)
        else:
            print("Unhandled option")
            sys.exit(1)

    if args[0] == "reset":
        if len(args) > 1:
            remove_node(args[1])
        else:
            reset_current_installation()
    else:
        if len(args) <= 0:
            print("Please provide a connection string.")
            usage()
            sys.exit(4)

        connection_parts = args[0].split("/")
        if is_node_running_dqlite():
            join_dqlite(connection_parts)
        else:
            join_etcd(connection_parts)

    sys.exit(0)
