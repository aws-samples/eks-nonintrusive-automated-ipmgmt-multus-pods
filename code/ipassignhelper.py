#!/usr/bin/env python3
import requests
import boto3, json
import sys, datetime
import netaddr
from netaddr import *
from requests.packages.urllib3 import Retry
import subprocess,copy,time
from collections import defaultdict
from multiprocessing import Process

### print logs
def tprint(caller="undef",var=""):
    print (datetime.datetime.now(),"-",caller,"-",var)

## This function gets the metadata token
def get_metadata_token():
    token_url="http://169.254.169.254/latest/api/token"
    headers = {'X-aws-ec2-metadata-token-ttl-seconds': '21600'}
    r= requests.put(token_url,headers=headers,timeout=(2, 5))
    return r.text

### This Function fetches the instanceid , region and availabilityzone from the hosting instance.
def getInstanceMetadata():
    instance_identity_url = "http://169.254.169.254/latest/dynamic/instance-identity/document"
    hostname_url="http://169.254.169.254/latest/meta-data/hostname"
    headers=None
    session = requests.Session()
    retries = Retry(total=3, backoff_factor=0.3)
    metadata_adapter = requests.adapters.HTTPAdapter(max_retries=retries)
    session.mount("http://169.254.169.254/", metadata_adapter)
    try:
        r = requests.get(instance_identity_url, timeout=(2, 5))
        code=r.status_code
        if code == 401: ###This node has IMDSv2 enabled, hence unauthorzied, we need to get token first and use the token
            tprint("node has IMDSv2 enabled!! Fetching Token first")
            token=get_metadata_token()
            headers = {'X-aws-ec2-metadata-token': token}
            r = requests.get(instance_identity_url, headers=headers, timeout=(2, 5))
            code=r.status_code
        if code == 200:
            response_json = r.json()
            instanceid = response_json.get("instanceId")
            region = response_json.get("region")
            az=response_json.get("availabilityZone")
            if headers : ## if the headers variables are are set in above step, then this nodes has IMDSv2 enabled, then use the metadata-token header
               s = requests.get(hostname_url, headers=headers,timeout=(2, 5))
            else:
               s = requests.get(hostname_url, timeout=(2, 5))
            hostname=s.text
            return(instanceid,region,hostname,az)
    except (requests.exceptions.ConnectTimeout, requests.exceptions.ConnectionError) as err:
        tprint("Execption: Connection to AWS EC2 Metadata timed out: " + str(err.__class__.__name__))
        tprint("Execption: Is this an EC2 instance? Is the AWS metadata endpoint blocked? (http://169.254.169.254/)")
        raise
    except Exception as e:
        tprint("Execption: caught exception " + str(e.__class__.__name__))
        raise    
###This function runs the shell command and retuns the output of the command
def shell_run_cmd_old(cmd):
    #p = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,encoding="utf-8")
    p = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,encoding="utf-8")
    try:
        stdout, stderr = p.communicate(timeout=30)
        retCode = p.returncode
        if retCode != 0:
            tprint("func:shell_run_cmd_old","retCode: "+str(retCode)  + " output: "+ stdout + " Error:" + stderr )
    except subprocess.TimeoutExpired:
        p.kill()
        stdout, stderr = p.communicate()    
    except Exception as e:
        print("Got exception" + str(e))            
    return stdout

###This function runs the shell command and retuns the output of the command
def shell_run_cmd(cmd,retCode=0,timeout=5):
    retCode=0
    err=""
    output=""
    try:
        output = subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=timeout)    
    except(subprocess.CalledProcessError) as e:   
        retCode = e.returncode
        tprint("func:shell_run_cmd:", str(e))
    return output

###This function describes a given subnet by its ID and returns the IPv4 Cidr of the subnet
def get_subnet_cidr(ec2_client,subnetId):
    response = ec2_client.describe_subnets(
        SubnetIds=[
            subnetId,
        ],    
    )
    for i in response['Subnets']:
        CidrBlock = i['CidrBlock']
    return  CidrBlock

###  This function fetches the ENI ID and corresponding ipv4 cidr of the subnet and populates 
#   the instacneData Dictionary object     
def get_instanceDetails(ec2_client,instance_id,instanceData):
    response = ec2_client.describe_instances(
        InstanceIds= [ instance_id ]
    )
    for r in response['Reservations']:
      for i in r['Instances']:
        for j in i["NetworkInterfaces"]:
            cidrBlock = get_subnet_cidr(ec2_client,j["SubnetId"])
            instanceData[cidrBlock] = j["NetworkInterfaceId"]
            tprint("func:get_instanceDetails:","Node ENIC: "+ j["NetworkInterfaceId"] + " cidr: " + cidrBlock  + " subnetID: " + j["SubnetId"])

### This function reads the Tags on the hosted EC2 instacne            
def get_instanceTags(ec2_client,instance_id,tags):
    response = ec2_client.describe_instances(
        InstanceIds= [ instance_id ]
    ) 
    for r in response['Reservations']:
      for i in r['Instances']:
        for j in i["Tags"]:
            tags[j["Key"]] = j["Value"]

### This function identifies an ENI from its mac-address and returns the ENI id            
def get_enic_bymac(ec2_client,ip,macaddress):
    enicId = ""
    response = ec2_client.describe_network_interfaces(
        Filters=[
            {
                'Name': 'mac-address',
                'Values': [
                    macaddress,
                ]
            },
        ],
    )
    for r in response['NetworkInterfaces']:
        enicId = r['NetworkInterfaceId']
        for x in r['PrivateIpAddresses']:
            if x['PrivateIpAddress'] == ip :
                tprint("func:get_enic_bymac:",ip + " already attached, no attachment needed") 
                enicId = ""
                break        
    return enicId

### This function fetches the ENI ID from an instance based on the subnetID and retruns the first matched ENI 
#   matching the subnet. Assumption is that a particular subnet has just 1 attachment.                
def get_enic(ec2_client,instance_id,subnet_id):
    response = ec2_client.describe_instances(
        InstanceIds=[
            instance_id,
        ],
    )
    for r in response['Reservations']:
       for i in r['Instances']:
        for j in i['NetworkInterfaces']:
            if j['SubnetId'] == subnet_id:
                enic_id = j['NetworkInterfaceId']
                tprint("func:get_enic:","found the interfaceID: " + enic_id + " for subnet: " + subnet_id)
                break
                
    return enic_id
    
### This function, first fetches an ENI which is hosting the given ipv6 addresses, later it releases those from the ENI   
#          
def release_ipv6(ip6List,subnet_cidr,client):
    tprint("func:release_ipv6","Going to release ip6List: " + str(ip6List))     
    
    response = client.describe_network_interfaces(
        Filters=[
            {
                'Name': 'ipv6-addresses.ipv6-address',
                'Values': ip6List
            },
        ],
    )
    if response['NetworkInterfaces'] == []:
        tprint("func:release_ipv6","ENI of ipv6 not attached yet, no need to release")
    else:
        for j in response['NetworkInterfaces']:
            network_interface_id = j['NetworkInterfaceId']
            response = client.unassign_ipv6_addresses(
                Ipv6Addresses=ip6List,
                NetworkInterfaceId = network_interface_id
            )
    tprint("func:release_ipv6","Finished releasing ip6List: " + str(ip6List))     

### This function assigns secondary IP v4 Addresses to an ENI. AllowReassignment = True ensures that it can fetch it by force
#   if these IPs are assigned as secondary IPs to another ENI.       
def assign_ip_to_nic(ipList,network_interface_id,client):  
    tprint("func:assign_ip_to_nic","Going to reassign iplist: " + str(ipList) + " to ENI:" +network_interface_id )    

    response = client.assign_private_ip_addresses(
        AllowReassignment=True,
        NetworkInterfaceId=network_interface_id,
        PrivateIpAddresses = ipList    
        )
    tprint("func:assign_ip_to_nic","Finished reassign iplist: " + str(ipList) + " to ENI:" +network_interface_id )    

### This function assigns secondary IP v6 Addresses to an ENI.        
def assign_ip6_to_nic(ip6List,network_interface_id,client):  
    tprint("func:assign_ip6_to_nic","Going to assign ip6List: " + str(ip6List) + " to ENI:" +network_interface_id )     
    response = client.assign_ipv6_addresses(
        Ipv6Addresses=ip6List,
        NetworkInterfaceId=network_interface_id,
        )
    tprint("func:assign_ip6_to_nic","Finished assign ip6List: " + str(ip6List) + " to ENI:" +network_interface_id ) 

### This function performs parallel asynchronous invocation to reassign the ipv4 secondary addresses on respective ENIs       
def manageParallelIPv4(ipmap,nicMap,ec2ClientArr):
    procipv4 = []   
    for  key in ipmap:    
        p = Process(target=assign_ip_to_nic, args=(ipmap[key],nicMap[key],ec2ClientArr[key]))
        p.start()
        procipv4.append(p)                    

    for p in procipv4:
        p.join(2)    
    tprint ("func:manageParallelIPv4:","Finished all IPV4")     

### This function performs parallel asynchronous invocation to release ipv6 from old ENIs & assign the ipv6 secondary 
#   addresses on respective ENIs       
def manageParallelIPv6(ip6map,nicMap,ec2ClientArr):
    procipv6 = []                   
    for  key in ip6map:        
        p = Process(target=release_ipv6, args=(ip6map[key],key,ec2ClientArr[key]))
        p.start()
        procipv6.append(p) 
    for p in procipv6:
        p.join(2)                    
    for  key in ip6map:      
        p = Process(target=assign_ip6_to_nic, args=(ip6map[key],nicMap[key],ec2ClientArr[key])) 
        p.start()
        procipv6.append(p) 
    for p in procipv6:
        p.join(2)                           
    tprint ("func:manageParallelIPv6:","Finished all IPv6")         
##Main Usage <scriptName> initContainers|sidecar