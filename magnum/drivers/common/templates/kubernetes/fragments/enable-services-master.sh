#!/bin/sh

. /etc/sysconfig/heat-params

ssh_cmd="ssh -F /srv/magnum/.ssh/config root@localhost"

# make sure we pick up any modified unit files
$ssh_cmd systemctl daemon-reload

# if the certificate manager api is enabled, wait for the ca key to be handled
# by the heat container agent (required for the controller-manager)
while [ ! -f /etc/kubernetes/certs/ca.key ] && \
    [ "$(echo $CERT_MANAGER_API | tr '[:upper:]' '[:lower:]')" == "true" ]; do
    echo "waiting for CA to be made available for certificate manager api"
    sleep 2
done

echo "starting services"
for service in etcd docker kube-apiserver kube-controller-manager kube-scheduler kubelet kube-proxy; do
    echo "activating service $service"
    $ssh_cmd systemctl enable $service
    $ssh_cmd systemctl restart $service
done

# Label self as master
until  [ "ok" = "$(curl --silent http://127.0.0.1:8080/healthz)" ] && \
    kubectl patch node ${INSTANCE_NAME} \
        --patch '{"metadata": {"labels": {"node-role.kubernetes.io/master": ""}}}'
do
    echo "Trying to label master node with node-role.kubernetes.io/master=\"\""
    sleep 5s
done
