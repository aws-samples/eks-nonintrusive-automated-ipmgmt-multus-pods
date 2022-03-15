import requests,subprocess,copy,time
import boto3, json
import sys,os
from netaddr import *
from requests.packages.urllib3 import Retry
from kubernetes.client.rest import ApiException
from kubernetes import client, config
from pprint import pprint
from ipassignhelper import *

### Class, representing a worker node for EKS
class WorkerNodeManager(object):
    def __init__(self):
        ###Featch instanceId, hostname AZ and region details
        data = getInstanceMetadata()
        tprint(data)
        self.instance_id = data[0]
        self.region = data[1]
        self.hostname=data[2]
        self.az=data[3]
        self.NetworkingData={}
        self.ec2ClientArr={}
        self.tags={}
        self.clusterName=None        
        if not self.instance_id or not self.region or not self.hostname:
            Exception("WorkerNodeManager: __init__" + " failed to get instanceid,hostname region data from worker metadata")
        ec2_client = boto3.client('ec2', region_name=self.region)
        ### Featch ENIs on the instance and corresponding ipv4 cidrs

        get_instanceDetails(ec2_client,self.instance_id,self.NetworkingData)
        for cidr in self.NetworkingData:
            k = boto3.client('ec2', region_name=self.region)
            self.ec2ClientArr[cidr] = k
        ### Featch instance Tags    
        get_instanceTags(ec2_client,self.instance_id,self.tags)    
        self.clusterName=getEKSClusterNameFromTag(self.tags)
    def getInstaceId(self):
        return self.instance_id          
    def getRegion(self):
        return self.region
    def getHostname(self):
        return self.hostname
    def getEKSClusterName(self):
        return self.clusterName    
    def getNetworkingData(self):
        return self.NetworkingData   
    def getEc2ClientArr(self):
        return  self.ec2ClientArr    

### Class representing Kubenrnetes/EKS manager & fetches kubernetes/EKS related details  
# This class is used to run kubernetes commands on the EKS cluster
class Kubernetesmanager(object):
    def __init__(self,region,cluster,roleARN=None):
        self.region=region
        self.cluster= cluster
        ### update the kubeconfig with EKS cluster details
        if roleARN:
             kubeconfigcmd="aws eks --region " + self.region+ " update-kubeconfig --name "+ self.cluster + " --role-arn " + roleARN           
        else:    
            kubeconfigcmd="aws eks --region " + self.region+ " update-kubeconfig --name "+ self.cluster
        tprint("Kubernetesmanager:__init__","EKS command:" + kubeconfigcmd)    
        output = shell_run_cmd_old(kubeconfigcmd)
        if output:
            contexts, active_context = config.list_kube_config_contexts()
            if not active_context:
                raise Exception("Kubernetesmanager:__init__","Fatal!!! couldnt set active kubernetes context, kubeconfig is not set")  
        else:
                raise Exception("Kubernetesmanager:__init__","Fatal!!! Got Error for " + kubeconfigcmd)  
        self.active_context = active_context
        config.load_kube_config()
        self.api_instance = client.CoreV1Api()
    ### function to refresh the kubeconfig token    
    def refresh(self):
        config.load_kube_config()
        self.api_instance = client.CoreV1Api()  

    ### function to fetch the multus IP addresses & the mac-addresses from a pod (with a namespace) which has the multus annotations 
    #    k8s.v1.cni.cncf.io/networks-status     

    def getMultusIps(self,name,namespace,ipAddress):
        pretty = 'pretty_example' # str | If 'true', then the output is pretty printed. (optional)
        try:
            api_response = self.api_instance.read_namespaced_pod(name, namespace, pretty=pretty)  
            js = json.loads(api_response.metadata.annotations['k8s.v1.cni.cncf.io/networks-status'])
            for i in js:
                if  'interface' in i:
                    for j in i['ips'] : 
                        ipAddress[j] = i['mac']              
        except ApiException as e:
            tprint("Kubernetesmanager:getMultusIps","Exception when calling CoreV1Api->read_namespaced_pod: %s\n" % e)        
            config.load_kube_config()
            self.api_instance = client.CoreV1Api()
### class to handle Multus IP addresses and related handlings            
class MultusHandler(object):
    def __init__(self,workerName,region,cluster,roleARN=None):
        self.workerName=workerName
        self.myKbernetesmgr=Kubernetesmanager(region=region,cluster=cluster,roleARN=roleARN)
        self.cmd="kubectl get pods -o=jsonpath='{range .items[?(@.metadata.annotations.k8s\.v1\.cni\.cncf\.io/networks)]}{.metadata.name}{\" \"}{@.metadata.namespace}{\" \"}{@.spec.nodeName}{\"\\n\"}' "
        self.multusNadcmd="kubectl get net-attach-def -o=jsonpath='{range .items[*]}{.metadata.name}{\" \"}{.metadata.namespace}{\"\\n\"}' "
        self.multusPods={}    
        self.multsNads={}
        self.multusNs=set()
    def refresh(self):
        self.myKbernetesmgr.refresh()

    ### Function to get multus NetworkAttachmentDefinitions across all the namespaces/given namespace
    def getMultusNads(self,ns="--all-namespaces"):    
        try:   
            if ns=="--all-namespaces":
                output = shell_run_cmd_old(self.multusNadcmd + "--all-namespaces") #shell_run_cmd_old(cmd,retCode)
            else: 
                output = shell_run_cmd_old(self.multusNadcmd + " -n "+ ns) #shell_run_cmd_old(cmd,retCode)
            if output:
                output = output.rstrip()
                allNadList=output.split("\n")
                self.multsNads.clear()
                for line in allNadList:
                    data=line.split(" ")
                    if len(data) > 1:
                        self.multsNads[data[0]] = { "namespace" : data[1] }
                        self.multusNs.add(data[1])
                    else:
                        raise Exception(line + " doesnt contain all 2 fileds, name namespace")  
            else:
                tprint("MultusHandler:getMultusNads", "Empty NAD output" + output)
        except Exception as e:
            tprint ("MultusHandler:getMultusNads", "Exception:" + str(e) )       
        return self.multsNads   
    ### function to get all the namespaces which are hosting NetworkAttachmentDefinitions    
    def getmultusNS(self):   
        self.getMultusNads("--all-namespaces")      
        return self.multusNs
    def getMultusPodNamesOnWorker(self,nsSet=None):
        self.multusPods.clear()
        for ns in nsSet:
            try:   
                if ns=="--all-namespaces":
                    output = shell_run_cmd_old(self.cmd + "--all-namespaces") #shell_run_cmd_old(cmd,retCode)
                else: 
                    output = shell_run_cmd_old(self.cmd + " -n "+ ns) #shell_run_cmd_old(cmd,retCode)                
                if output:
                    output = output.rstrip()
                    allPodList=output.split("\n")
                    #self.multusPods.clear()
                    for line in allPodList:
                        data=line.split(" ")
                        if len(data) > 2:
                            if self.workerName == data[2]:
                                ipAddress={}
                                self.myKbernetesmgr.getMultusIps(name=data[0],namespace=data[1],ipAddress=ipAddress)
                                self.multusPods[data[0]] = { "namespace" : data[1] , "ipAddress": ipAddress }
                        else:
                            raise Exception(line + " doesnt contain all 3 fileds, podname namespace workername")    
                else:
                    tprint("MultusHandler:getMultusPodNamesOnWorker", "Empty  Multus Pod list " + output)
            except Exception as e:
                tprint ("MultusHandler:getMultusPodNamesOnWorker","Exception" + str(e))       
        return self.multusPods
class MultusPod(object):
    def __init__(self,name,namespace,ipDict):
        self.ipDict=ipDict
        self.currIPList=list(ipDict.keys())
        self.prevIPList=[]
        self.name=name
        self.namespace=namespace
    def getName(self):
        return self.name
    def getNamespace(self):
        return self.namespace
    def getcurrIPList(self):
        return self.currIPList
    def setcurrIPList(self,currIPList):
        self.currIPList = currIPList       
    def getprevIPList(self):
        return self.prevIPList       
    def setprevIPList(self,prevIPList):
        self.prevIPList = prevIPList  
    def __str__(self) :
        return self.name+ " " +self.namespace + str(self.ipDict)                 
def getEKSClusterNameFromTag(tags):
    clusterName=None
    any(key.startswith("kubernetes.io/cluster/") for key in tags)
    for key in tags.keys():
        if key.startswith("kubernetes.io/cluster/"):
            clusterName=key.split("kubernetes.io/cluster/",1)[1]
    return clusterName


##MAIN
usePodMACtoIdentifyENIC = 0
#podIpAddress = {}
currIPList = []
currPods={}
multusPods={}
clsuterIAmRole=None
podSearchQuery="ALL"
multusNs=None

try:
    if 'EKS_CLUSTER_ROLEARN' in os.environ:
        clsuterIAmRole=os.environ['EKS_CLUSTER_ROLEARN']
    if 'PODSEARCHQUERY'in os.environ:
        podSearchQuery=os.environ['PODSEARCHQUERY']
    myWorkerNode=WorkerNodeManager()
    tprint("func:Main",myWorkerNode.getHostname() + " " + myWorkerNode.getEKSClusterName() + " " + "IAM Role:"+str(clsuterIAmRole))
    myMultusMgr= MultusHandler(workerName=myWorkerNode.getHostname(),region=myWorkerNode.getRegion(),cluster=myWorkerNode.getEKSClusterName(),roleARN=clsuterIAmRole)
    workerENIData=myWorkerNode.getNetworkingData()
    ec2ClientArr=myWorkerNode.getEc2ClientArr()
except Exception as e:
    tprint("func:Main",str(e)+ " Exiting!!")    
    exit(1)

ctrRefreshTime=300 
tokenRefreshTime=30   
ctr=0    
while(1):
    if (ctr % tokenRefreshTime) == 0:
        myMultusMgr.refresh()
    if ctr == 0 :
         tprint("func:Main","Preiodic check .. Log Entry before the multus query, to check the query time taken")
    if podSearchQuery != "ALL":     
        multusNs=myMultusMgr.getmultusNS()
    if multusNs:
        multusPods=myMultusMgr.getMultusPodNamesOnWorker(nsSet=multusNs)
    else:
        allns={"--all-namespaces"}
        multusPods=myMultusMgr.getMultusPodNamesOnWorker(allns)
    if ctr == 0 :
         tprint("func:Main","Preiodic check .. Log Entry after the multus query, to check the query time taken")   
    for pod in multusPods.keys():
        newIPList=list(multusPods[pod]["ipAddress"].keys())
        obj= MultusPod(pod,multusPods[pod]["namespace"],multusPods[pod]["ipAddress"])
        ipmap = defaultdict(list)
        ip6map = defaultdict(list)
        noChange=True       
        if pod in currPods.keys():
            if set(currPods[pod].getcurrIPList()) == set(newIPList):
                pass #tprint("func:Main", "No IP change for pod " + pod)
            else:
                noChange=False
                tprint("func:Main", "IP change for pod " + str(newIPList))
        else:
            noChange=False
        if noChange==False:    
            tprint("func:Main","working on pod :" + str(obj))
            for ipaddress in newIPList:
                for cidr in workerENIData.keys():  
                    if IPAddress(ipaddress) in list(IPNetwork(cidr)):                   
                        if  netaddr.valid_ipv4(str(ipaddress)):    
                            ipmap[cidr].append(str(ipaddress))
                        else :
                            ip6map[cidr].append(str(ipaddress))
            manageParallelIPv4(ipmap,workerENIData,ec2ClientArr)
            currPods[pod]=obj  
    ctr=ctr+1   
    if ctr == ctrRefreshTime:
        ctr=0     
    time.sleep(1)    