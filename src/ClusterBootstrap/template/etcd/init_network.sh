curl  --cacert /etc/kubernetes/ssl/etcd/ca.pem --cert /etc/kubernetes/ssl/etcd/etcd.pem --key /etc/kubernetes/ssl/etcd/etcd-key.pem -X PUT -d "value={\"Network\":\"{{cnf["pod_ip_range"]}}\",\"Backend\":{\"Type\":\"vxlan\"}}" "https://127.0.0.1:2379/v2/keys/coreos.com/network/config"