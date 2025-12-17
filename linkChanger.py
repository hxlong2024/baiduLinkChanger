import streamlit as st
import requests
from retrying import retry
import time
import re
import random
import string
import traceback
from typing import Union, List, Any

# ==========================================
# 第一部分：配置与常量
# ==========================================

BASE_URL = 'https://pan.baidu.com'
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36',
    'Referer': 'https://pan.baidu.com',
}

# 严格的非法字符正则：除了 汉字、字母、数字、下划线、横线、空格 以外的全部视为非法
INVALID_CHARS_REGEX = re.compile(r'[^\u4e00-\u9fa5a-zA-Z0-9_\-\s]')


# ==========================================
# 第二部分：核心工具函数
# ==========================================

def sanitize_filename(name: str) -> str:
    """
    强力清洗文件名
    只保留：中文、英文、数字、下划线、横线、空格
    去除：Emoji、特殊符号(【】[]()...)、控制符等
    """
    if not name: return ""
    # 替换常见干扰符为空格
    name = re.sub(r'[【】\[\]()]', ' ', name)
    # 替换所有非白名单字符为空字符串
    clean_name = INVALID_CHARS_REGEX.sub('', name)
    # 将连续空格合并为一个，并去除首尾空格
    clean_name = re.sub(r'\s+', ' ', clean_name).strip()
    return clean_name


def extract_folder_name(full_text: str, match_start: int) -> str:
    """智能提取文件夹名称"""
    lookback_limit = max(0, match_start - 200)
    pre_text = full_text[lookback_limit:match_start]
    lines = pre_text.splitlines()

    candidate_name = ""
    for line in reversed(lines):
        clean_line = line.strip()
        if not clean_line: continue
        # 跳过纯提示词行
        if re.match(r'^(百度|链接|提取码|:|：|https?|夸克)*$', clean_line, re.IGNORECASE):
            continue

        # 移除行内的干扰词
        clean_line = re.sub(r'(百度|链接|提取码|:|：|pwd|夸克).*$', '', clean_line, flags=re.IGNORECASE).strip()

        if clean_line:
            candidate_name = clean_line
            break

    # 清洗名字
    final_name = sanitize_filename(candidate_name)

    # 如果清洗后名字太短或为空，返回None(指示后续使用默认名)
    if not final_name or len(final_name) < 2:
        return None

    return final_name[:50]  # 截断长度


def clean_quark_links(text: str) -> str:
    """剔除夸克网盘链接及其整行"""
    return re.sub(r'^.*pan\.quark\.cn.*$[\r\n]*', '', text, flags=re.MULTILINE)


def update_cookie(bdclnd: str, cookie: str) -> str:
    cookies_dict = dict(map(lambda item: item.split('=', 1), filter(None, cookie.split(';'))))
    cookies_dict['BDCLND'] = bdclnd
    return ';'.join([f'{key}={value}' for key, value in cookies_dict.items()])


def generate_code() -> str:
    return ''.join(random.choice(string.ascii_letters + string.digits) for _ in range(4))


def parse_response(content: str) -> Union[List[Any], int]:
    try:
        content_str = content.decode("utf-8")
    except:
        content_str = str(content)

    shareid = re.search(r'"shareid":(\d+?),', content_str)
    uk = re.search(r'"share_uk":"(\d+?)",', content_str)
    fs_id = re.findall(r'"fs_id":(\d+?),', content_str)

    if shareid and uk and fs_id:
        return [shareid.group(1), uk.group(1), fs_id, [], []]
    return -1


# ==========================================
# 第三部分：网络请求类
# ==========================================

class Network:
    def __init__(self):
        self.s = requests.Session()
        self.s.trust_env = False
        self.headers = HEADERS.copy()
        self.bdstoken = ''
        requests.packages.urllib3.disable_warnings()

    @retry(stop_max_attempt_number=3, wait_fixed=2000)
    def get_bdstoken(self) -> Union[str, int]:
        url = f'{BASE_URL}/api/gettemplatevariable'
        params = {'fields': '["bdstoken","token","uk","isdocuser"]'}
        try:
            r = self.s.get(url, params=params, headers=self.headers, verify=False)
            if 'errno' in r.json() and r.json()['errno'] != 0:
                return r.json()['errno']
            return r.json()['result']['bdstoken']
        except:
            return -1

    @retry(stop_max_attempt_number=3)
    def verify_pass_code(self, link: str, code: str) -> Union[str, int]:
        url = f'{BASE_URL}/share/verify'
        surl = re.search(r'(?:surl=|/s/1|/s/)([\w\-]+)', link)
        if not surl: return -9

        params = {
            'surl': surl.group(1),
            't': str(int(time.time() * 1000)),
            'bdstoken': self.bdstoken,
            'channel': 'chunlei', 'web': '1', 'clienttype': '0'
        }
        data = {'pwd': code, 'vcode': '', 'vcode_str': ''}
        r = self.s.post(url, params=params, data=data, headers=self.headers, verify=False)
        if r.json()['errno'] == 0:
            return r.json()['randsk']
        return r.json()['errno']

    def get_transfer_params(self, url: str) -> bytes:
        return self.s.get(url, headers=self.headers, verify=False).content

    @retry(stop_max_attempt_number=3)
    def create_dir(self, path: str) -> int:
        url = f'{BASE_URL}/api/create'
        data = {'path': path, 'isdir': '1', 'block_list': '[]'}
        params = {'a': 'commit', 'bdstoken': self.bdstoken}
        r = self.s.post(url, params=params, data=data, headers=self.headers, verify=False)
        return r.json()['errno']

    @retry(stop_max_attempt_number=5)
    def transfer_file(self, params_list: list, path: str) -> int:
        url = f'{BASE_URL}/share/transfer'
        data = {'fsidlist': f"[{','.join(params_list[2])}]", 'path': f'/{path}'}
        params = {'shareid': params_list[0], 'from': params_list[1], 'bdstoken': self.bdstoken}
        r = self.s.post(url, params=params, data=data, headers=self.headers, verify=False)
        return r.json()['errno']

    @retry(stop_max_attempt_number=3)
    def create_share(self, fs_id: str, pwd: str) -> Union[str, int]:
        url = f'{BASE_URL}/share/set'
        data = {'period': '0', 'pwd': pwd, 'fid_list': f'[{fs_id}]', 'schannel': '4'}
        params = {'bdstoken': self.bdstoken, 'channel': 'chunlei', 'clienttype': '0', 'web': '1'}
        r = self.s.post(url, params=params, data=data, headers=self.headers, verify=False)
        if r.json()['errno'] == 0:
            return r.json()['link']
        return r.json()['errno']

    def get_dir_fsid(self, parent_path: str, target_name: str) -> str:
        url = f'{BASE_URL}/api/list'
        params = {'dir': parent_path, 'bdstoken': self.bdstoken, 'order': 'time', 'desc': '1'}
        r = self.s.get(url, params=params, headers=self.headers, verify=False)
        if r.json()['errno'] == 0:
            for item in r.json()['list']:
                if item['server_filename'] == target_name:
                    return item['fs_id']
        return None


# ==========================================
# 第四部分：Streamlit 业务流程
# ==========================================

def process_single_link(network, match, full_text, root_path):
    url = match.group(1)

    # 1. 提取提取码
    pwd_match = re.search(r'(?:\?pwd=|&pwd=|\s+|提取码[:：]?\s*)([a-zA-Z0-9]{4})', match.group(0))
    pwd = pwd_match.group(1) if pwd_match else ""
    clean_url = url.split('?')[0]

    # 2. 智能提取文件夹名 (含严格清洗)
    folder_name = extract_folder_name(full_text, match.start())
    # 如果提取失败，使用默认时间戳名
    if not folder_name:
        folder_name = f"Resource_{int(time.time())}"
        st.write(f"⚠️ 无法提取有效名称，使用默认名: **{folder_name}**")
    else:
        st.write(f"📂 识别并净化资源名为: **{folder_name}**")

    # 3. 验证链接
    if pwd:
        res = network.verify_pass_code(clean_url, pwd)
        if isinstance(res, int):
            st.error(f"❌ 链接验证失败 ({clean_url}) 错误代码: {res}")
            return None
        network.headers['Cookie'] = update_cookie(res, network.headers['Cookie'])

    # 4. 获取参数
    content = network.get_transfer_params(clean_url)
    params = parse_response(content)
    if params == -1:
        st.error(f"❌ 链接解析失败 ({clean_url}) - 可能是死链或Cookie过期")
        return None

    # 5. 创建文件夹 & 转存 (核心修复逻辑：失败重试机制)

    # 尝试一：使用提取的名字 + 随机码
    safe_suffix = generate_code()
    final_folder_name = f"{folder_name}_{safe_suffix}"
    full_save_path = f"{root_path}/{final_folder_name}"

    network.create_dir(root_path)  # 确保根目录存在

    create_res = network.create_dir(full_save_path)

    # 如果创建失败（且不是因为文件夹已存在），则降级尝试
    if create_res != 0 and create_res != -8:
        st.warning(f"⚠️ 使用名称 '{final_folder_name}' 创建目录失败 (代码: {create_res})，尝试使用安全时间戳名称...")

        # 尝试二：完全安全的纯时间戳名称
        final_folder_name = f"Transfer_{int(time.time())}_{safe_suffix}"
        full_save_path = f"{root_path}/{final_folder_name}"
        create_res_retry = network.create_dir(full_save_path)

        if create_res_retry != 0 and create_res_retry != -8:
            st.error(f"❌ 目录创建彻底失败 (代码: {create_res_retry})，跳过此任务。")
            return None
        st.write(f"✅ 已切换为安全目录名: {final_folder_name}")

    # 执行转存
    transfer_res = network.transfer_file(params, full_save_path)
    if transfer_res != 0:
        st.error(f"❌ 转存文件失败 (代码: {transfer_res}) - 请检查网盘空间或文件数量限制")
        return None

    # 6. 分享
    fs_id = network.get_dir_fsid(f"/{root_path}", final_folder_name)
    if not fs_id:
        st.error("❌ 转存后无法获取文件夹ID，无法分享")
        return None

    new_pwd = generate_code()
    share_link = network.create_share(fs_id, new_pwd)

    if isinstance(share_link, int):
        st.error(f"❌ 创建分享链接失败 (代码: {share_link})")
        return None

    st.success(f"✅ 处理成功！")
    return f"{share_link}?pwd={new_pwd}"


# 回调函数：清除文本框状态
def clear_text():
    st.session_state["user_input"] = ""


def main():
    st.set_page_config(page_title="百度网盘转存助手", layout="wide")
    st.title("🔄 百度网盘智能转存 (修复版)")

    with st.sidebar:
        cookie = st.text_area("输入Cookie (必填)", height=150)
        root_path = st.text_input("网盘保存路径", value="我的自动转存资源")

    # 文本框绑定 key="user_input"，以便在 session_state 中管理
    input_text = st.text_area(
        "📝 输入文本",
        height=200,
        placeholder="粘贴包含链接的文本，程序将自动净化文件名并转存...",
        key="user_input"
    )

    # 按钮布局：一键清除 与 开始处理
    col1, col2 = st.columns([1, 6])

    with col1:
        st.button("🗑️ 一键清除", on_click=clear_text)

    with col2:
        start_process = st.button("🚀 开始处理", type="primary")

    if start_process:
        if not cookie:
            st.warning("请先输入 Cookie")
            st.stop()

        # 1. 预处理
        processed_text = clean_quark_links(input_text)

        network = Network()
        network.headers['Cookie'] = "".join(cookie.split())

        with st.status("正在自动化处理 (点击展开详情)...", expanded=True) as status:
            token = network.get_bdstoken()
            if isinstance(token, int):
                status.update(label=f"❌ Cookie 无效 (代码: {token})", state="error")
                st.stop()
            network.bdstoken = token

            link_regex = re.compile(r'(https?://pan\.baidu\.com/s/[a-zA-Z0-9_\-]+(?:\?pwd=[a-zA-Z0-9]+)?)')
            matches = list(link_regex.finditer(processed_text))

            if not matches:
                status.update(label="⚠️ 未找到百度网盘链接", state="complete")
                st.stop()

            final_text = processed_text
            success_count = 0

            # 倒序处理
            for match in reversed(matches):
                st.divider()  # 分隔线
                new_link = process_single_link(network, match, processed_text, root_path)
                if new_link:
                    start, end = match.span()
                    final_text = final_text[:start] + new_link + final_text[end:]
                    success_count += 1

            if success_count > 0:
                status.update(label=f"✅ 全部完成！成功处理 {success_count} 个链接", state="complete")
            else:
                status.update(label="⚠️ 处理完成，但没有成功转存任何链接", state="error")

        if success_count > 0:
            st.subheader("🎉 处理结果 (点击右上角复制)")
            st.code(final_text, language="text")


if __name__ == '__main__':
    main()