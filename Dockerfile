# gdb2pg Web 服务镜像
# 基础镜像：官方 GDAL 镜像（含 OpenFileGDB 读写、Python 绑定）
# 可用 --build-arg GDAL_IMAGE=... 覆盖（默认官方 ghcr.io/osgeo/gdal:ubuntu-small-3.10.2）
ARG GDAL_IMAGE=ghcr.io/osgeo/gdal:ubuntu-small-3.10.2
FROM ${GDAL_IMAGE}

WORKDIR /app

# 1) 先装依赖（利用层缓存）：基础镜像未带 pip，先 apt 安装；
#    GDAL 绑定由镜像提供，pip 装回同版本保证 Python 绑定一致
ARG GDAL_IMAGE
COPY requirements.txt ./
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3-pip \
 && rm -rf /var/lib/apt/lists/* \
 && python3 -m pip install --break-system-packages --no-cache-dir \
      gdal==3.10.2 -r requirements.txt

# 2) 拷贝工程（含已构建好的前端产物 gdb2pg/web/static）
COPY gdb2pg ./gdb2pg
COPY configs ./configs

# 3) 数据目录：zip 上传解压 + 目录浏览默认白名单根
RUN mkdir -p /app/uploads

ENV G2P_WEB_HOST=0.0.0.0 \
    G2P_WEB_PORT=8000 \
    G2P_WEB_UPLOADS_DIR=/app/uploads

EXPOSE 8000

# 业务库（管理库）连接通过环境变量注入（见 docker-compose.yml）
CMD ["python", "-m", "gdb2pg", "web"]