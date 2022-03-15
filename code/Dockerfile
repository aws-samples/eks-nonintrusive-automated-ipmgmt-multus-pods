# syntax=docker/dockerfile:1
#FROM amazon/aws-cli 
FROM public.ecr.aws/amazonlinux/amazonlinux:latest
WORKDIR /app
RUN yum -y install python3
RUN yum -y install zip unzip
RUN curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip"
RUN unzip awscliv2.zip
RUN ./aws/install
RUN curl -LO https://storage.googleapis.com/kubernetes-release/release/$(curl -s https://storage.googleapis.com/kubernetes-release/release/stable.txt)/bin/linux/amd64/kubectl
RUN chmod +x ./kubectl
RUN mv ./kubectl /usr/local/bin
COPY requirements.txt requirements.txt
COPY *.pyc /app/
RUN pip3 install  -r requirements.txt