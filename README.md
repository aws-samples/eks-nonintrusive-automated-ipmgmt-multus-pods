
## Daemonset based non-intruisive Multus pod IP management on EKS

For multus pods on EKS, primary pod interface is managed by VPC CNI, however secondary interfaces are managed by other CNIs like ipvlan and different ipams like "host-local" , "static" or "whereabouts" via multus meta-plugin. To make these secondary interfaces IPs routable in VPC network, the IP allocation needs to be done on the worker node ENI, handling the master interface for the multus network attachment. 

The blog [Automated Multus pod IP management on EKS](https://github.com/aws-samples/eks-automated-ipmgmt-multus-pods) explains how we can use the initContainer or sideCar container to automate this whole process. This is an efficient and fast mechanism to handle the multus pod IP addresses in EKS environment per multus workload basis, however one needs to modify the helm chart for multus workloads and add this additinal container in the charts. Also this needs to be maintained per releases.

This blog explains the procedure of automating this IP allocation with the help of a daemonset based solution, so that applications dont need to modify their helm charts and they can use deploy their IPVLAN based multus pods/charts in EKS ithout any change.  

Note: The solution might be slower to react, specially for a bigger cluster (800+ pods, 20+ workers) than the [pod based initContainer & sidecar container](https://github.com/aws-samples/eks-automated-ipmgmt-multus-pods), as this solution scans for multus pods in EKS clusters and assigns the needed secondary IPs on the worker ENI. 

### Daemonset based solution:

Daemonset based solution utilizes a deamonset which is installed once per cluster where multus (IPVALN) based workloads are getting deployed. As the name suggests, Daemonset based solution, each worker node will have a pod running, which will keep monitoring for the multus pods
hosted on that worker. Not only it watches for the newly deployed pod, even on the existing pods it monitors for a change in ip addresses (similar to sidecar solution). 

#### Pre-requisite

1. EKS cluster
2. Self managed nodegroup (minimum 2 workers) with secondary ENIs with desired IAM role & [IAM policy](samples/iam-policy.json).
3. Security group on the worker nodes have ICMP traffic allowed between worker nodes
4. multus CNI (along with ipvlan CNI) 
5. whereabouts IPAM CNI
6. Bastion node with docker and git

Note: If you are using the IMDSv2 enabled, then ensure that nodes have HttpPutResponseHopLimit as 2. Please refer to [Configure the instance metadata options](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/configuring-instance-metadata-options.html).    

#### How to Build

Clone this repo:

```
git clone https://github.com/aws-samples/eks-nonintrusive-automated-ipmgmt-multus-pods
```
Please replace the xxxxxxxxx with your accout id and also choose the region where your ECR repository is.


```
cd code
python3 -m compileall -b .
docker build --tag xxxxxxxxx.dkr.ecr.us-east-2.amazonaws.com/aws-ip-daemon:0.1 .
aws ecr get-login-password --region us-east-2 | docker login --username AWS --password-stdin xxxxxxxxx.dkr.ecr.us-east-2.amazonaws.com
aws ecr create-repository --repository-name aws-ip-daemon --region us-east-2
docker push xxxxxxxxx.dkr.ecr.us-east-2.amazonaws.com/aws-ip-daemon:0.1
```

####  Deploy Daemonset Solution

Please replace the xxxxxxxxx with your accout id and also choose the region where your ECR repository is.

```
cd samples
kubectl -n kube-system apply -f aws-ip-daemon.yaml
```

####  Deploy Sample Multus Workloads Solution


Deploy the sample multus workloads in samples direcory, use your account number in place of xxxxxxxxx, and test it. 
```
$ cd samples
$ kubectl create ns multus
namespace/multus created
$ kubectl -n multus apply -f multus-nad-wb.yaml
networkattachmentdefinition.k8s.cni.cncf.io/ipvlan-multus created
$ kubectl -n multus apply -f busybox-deployment.yaml
deployment.apps/busybox-deployment created
$ kubectl -n kube-system get po -o wide
NAME                       READY   STATUS    RESTARTS   AGE     IP           NODE                                       NOMINATED NODE   READINESS GATES
aws-ip-daemon-2lmk5        1/1     Running   0          39h     10.0.0.13    ip-10-0-0-176.us-east-2.compute.internal   <none>           <none>
aws-ip-daemon-2wmqs        1/1     Running   0          39h     10.0.0.174   ip-10-0-0-48.us-east-2.compute.internal    <none>           <none>
aws-ip-daemon-5tssn        1/1     Running   0          39h     10.0.0.216   ip-10-0-0-126.us-east-2.compute.internal   <none>           <none>
aws-ip-daemon-sl69j        1/1     Running   0          39h     10.0.0.249   ip-10-0-0-81.us-east-2.compute.internal    <none>           <none>
$ kubectl -n multus get po -o wide

NAME                                  READY   STATUS    RESTARTS   AGE     IP           NODE                                       NOMINATED NODE   READINESS GATES
busybox-deployment-5484b6c9cd-d9vvl   1/1     Running   0          2m12s   10.0.0.132   ip-10-0-0-48.us-east-2.compute.internal    <none>           <none>
busybox-deployment-5484b6c9cd-r9kxp   1/1     Running   0          2m12s   10.0.0.50    ip-10-0-0-126.us-east-2.compute.internal   <none>           <none>
busybox-deployment-5484b6c9cd-tqwzj   1/1     Running   0          2m12s   10.0.0.201   ip-10-0-0-176.us-east-2.compute.internal   <none>           <none>

$ kubectl -n multus exec -it busybox-deployment-5484b6c9cd-d9vvl -- ip a | grep -B1 "global net1"
    link/ether 02:b6:0f:c1:7b:54 brd ff:ff:ff:ff:ff:ff
    inet 10.10.1.81/24 brd 10.10.1.255 scope global net1
$ kubectl -n multus exec -it busybox-deployment-5484b6c9cd-r9kxp -- ip a | grep -B1 "global net1"
    link/ether 02:8d:a4:f6:c1:9e brd ff:ff:ff:ff:ff:ff
    inet 10.10.1.82/24 brd 10.10.1.255 scope global net1
$
$ kubectl -n multus exec -it busybox-deployment-5484b6c9cd-r9kxp -- ping -c 1 10.10.1.81
PING 10.10.1.81 (10.10.1.81): 56 data bytes
64 bytes from 10.10.1.81: seq=0 ttl=255 time=0.216 ms

--- 10.10.1.81 ping statistics ---
1 packets transmitted, 1 packets received, 0% packet loss
round-trip min/avg/max = 0.216/0.216/0.216 ms
$
```

## Cleanup
```
$ kubectl -n multus delete -f busybox-deployment.yaml
$ kubectl -n multus delete -f aws-ip-daemon.yaml
$ kubectl delete ns multus
```

## Conclusion
This blogs shows how this daemonset can solve the multus based pod routing in VPC, without any modification in the helm charts of the applications. The daemonset sample solution handles it in non-intruisive way. This sample can be further optimized for the speed and scalability by the users for the bigger deployments.

## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information.

## License

This library is licensed under the MIT-0 License. See the LICENSE file.

