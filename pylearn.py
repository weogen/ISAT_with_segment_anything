import os
print("PyCharm 环境变量:")
print(os.environ.get('LD_LIBRARY_PATH'))
print(os.environ.get('PATH'))


# 创建generate_ts.py文件
import os
from PyQt5.QtCore import QTranslator, QLibraryInfo

def generate_translation():
    # 获取系统Qt路径
    qt_bin = QLibraryInfo.location(QLibraryInfo.BinariesPath)
    lupdate = os.path.join(qt_bin, 'lupdate')
    
    if not os.path.exists(lupdate):
        raise FileNotFoundError(f"lupdate not found at {qt_bin}")
    
    # 生成命令
    cmd = f'"{lupdate}" MainWindow.py -ts zh_CN.ts'
    os.system(cmd)

if __name__ == '__main__':
    generate_translation()