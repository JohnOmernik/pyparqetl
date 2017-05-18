FROM ubuntu:latest

WORKDIR /app

RUN mkdir -p /app/code && mkdir -p /app/data

RUN wget -O - http://apt.llvm.org/llvm-snapshot.gpg.key | apt-key add -
RUN echo "deb http://apt.llvm.org/xenial/ llvm-toolchain-xenial-3.9 main" > /etc/apt/sources.list.d/LLVM.list
RUN echo "deb-src http://apt.llvm.org/xenial/ llvm-toolchain-xenial-3.9 main" >> /etc/apt/sources.list.d/LLVM.list

RUN apt-get update && apt-get install -y python3 wget python3-dev python3-setuptools python3-pip zlib1g-dev libevent-pthreads-2.0-5 libssl-dev libsasl2-dev liblz4-dev libsnappy1v5 libsnappy-dev liblzo2-2 liblzo2-dev clang-3.9 lldb-3.9 && apt-get clean && apt-get autoremove -y

RUN wget https://github.com/edenhill/librdkafka/archive/v0.9.4.tar.gz && tar zxf v0.9.4.tar.gz && cd librdkafka-0.9.4 && ./configure && make && make install && ldconfig && cd .. && rm -rf librdkafka-0.9.4 && rm v0.9.4.tar.gz

ENV LLVM_CONFIG="/usr/lib/llvm-3.9/bin/llvm-config"

RUN pip3 install numpy pandas pytest python-snappy python-lzo brotli

RUN pip3 install cython confluent-kafka confluent-kafka[avro] kazoo

RUN pip3 install numba

RUN pip3 install git+https://github.com/dask/fastparquet

ADD pyparq.py /app/code/

CMD ["/bin/bash"]
