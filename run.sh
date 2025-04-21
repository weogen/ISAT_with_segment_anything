
# 1.在终端打开
# # 创建项目专属启动脚本
# echo '#!/bin/bash
# pyenv activate venv_ISAT_torch-2.2.1_cp38
# export LD_LIBRARY_PATH="/home/wegn/.pyenv/versions/venv_ISAT_torch-2.2.1_cp38/lib/python3.8/site-packages/nvidia/cudnn/lib:$LD_LIBRARY_PATH"
# python main.py "$@"' > run.sh

# # 添加执行权限
# chmod +x run.sh

# # 运行程序
# ./run.sh


# 2.在激活了的python虚拟环境打开
unset LD_LIBRARY_PATH

python main.py "$@"