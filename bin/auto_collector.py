import ctypes, re, sys, html, time, os, requests
from ctypes import wintypes
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

OUT_DIR = Path("D:/hv/resource/articles_paparazzi")
OUT_DIR.mkdir(parents=True, exist_ok=True)
URLS_LOG = Path("D:/hv/resource/collected_urls.txt")

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.OpenClipboard.restype = wintypes.BOOL
user32.CloseClipboard.restype = wintypes.BOOL
user32.GetClipboardData.argtypes = [wintypes.UINT]
user32.GetClipboardData.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
kernel32.GlobalUnlock.restype = wintypes.BOOL

VK_CONTROL = 0x11
VK_NEXT = 0x22 # Page Down
KEYEVENTF_KEYUP = 0x0002

def press_combo(modifier, key):
    user32.keybd_event(modifier, 0, 0, 0)
    user32.keybd_event(key, 0, 0, 0)
    time.sleep(0.05)
    user32.keybd_event(key, 0, KEYEVENTF_KEYUP, 0)
    user32.keybd_event(modifier, 0, KEYEVENTF_KEYUP, 0)

def press_key(key):
    user32.keybd_event(key, 0, 0, 0)
    time.sleep(0.05)
    user32.keybd_event(key, 0, KEYEVENTF_KEYUP, 0)

def extract_urls_from_clipboard():
    CF_HTML = user32.RegisterClipboardFormatW('HTML Format')
    CF_UNICODETEXT = 13
    urls = []
    if user32.OpenClipboard(None):
        try:
            h = user32.GetClipboardData(CF_HTML)
            if h:
                ptr = kernel32.GlobalLock(h)
                if ptr:
                    try:
                        raw = ctypes.string_at(ptr).decode('utf-8', errors='ignore')
                        matches = re.findall(r'https://mp\.weixin\.qq\.com/s[^\x00"\'<>\s]+', raw)
                        urls.extend(matches)
                    finally:
                        kernel32.GlobalUnlock(h)
            h_txt = user32.GetClipboardData(CF_UNICODETEXT)
            if h_txt:
                ptr_txt = kernel32.GlobalLock(h_txt)
                if ptr_txt:
                    try:
                        txt = ctypes.wstring_at(ptr_txt)
                        matches = re.findall(r'https://mp\.weixin\.qq\.com/s[^\x00"\'<>\s]+', txt)
                        urls.extend(matches)
                    finally:
                        kernel32.GlobalUnlock(h_txt)
        finally:
            user32.CloseClipboard()
    
    cleaned = []
    for u in urls:
        u = html.unescape(u).split('#')[0]
        if u not in cleaned and "mp.weixin.qq.com/s" in u:
            cleaned.append(u)
    return cleaned

def clean_wechat_html(raw_html: str) -> str:
    cleaned = re.sub(r'<script[^>]*>.*?</script>', '', raw_html, flags=re.DOTALL)
    cleaned = re.sub(r'<style[^>]*>.*?</style>', '', cleaned, flags=re.DOTALL)
    cleaned = re.sub(r'<p[^>]*>', '\n\n', cleaned)
    cleaned = re.sub(r'<br\s*/?>', '\n', cleaned)
    cleaned = re.sub(r'<[^>]+>', '', cleaned)
    cleaned = html.unescape(cleaned)
    lines = [line.strip() for line in cleaned.splitlines()]
    cleaned_lines = []
    for line in lines:
        if not line:
            if cleaned_lines and cleaned_lines[-1] != "":
                cleaned_lines.append("")
            continue
        if "var first_sceen__time" in line or "document.getElementById('js_content')" in line:
            continue
        cleaned_lines.append(line)
    return "\n".join(cleaned_lines).strip()

def download_and_save(url: str) -> bool:
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
        r = requests.get(url, headers=headers, timeout=12)
        if r.status_code != 200:
            print(f"  [HTTP {r.status_code}] 无法访问: {url}")
            return False

        m_title = re.search(r'<meta property="og:title" content="([^"]*)"', r.text)
        title = m_title.group(1).strip() if m_title else "未命名"
        title_clean = re.sub(r'[\\/*?:"<>|]', '_', title)

        m_date = re.search(r'var\s+createTime\s*=\s*["\']?(\d+)["\']?', r.text)
        if m_date:
            ts = int(m_date.group(1))
            date_str = time.strftime("%Y-%m-%d", time.localtime(ts)) if ts > 1000000 else str(ts)
        else:
            date_str = "unknown"

        out_file = OUT_DIR / f"{date_str}_{title_clean}.md"
        if out_file.exists() and out_file.stat().st_size > 500:
            print(f"  [跳过已存在] 《{title}》")
            return True

        m_content = re.search(r'<div[^>]*id="js_content"[^>]*>(.*?)</div>', r.text, re.DOTALL)
        if not m_content:
            m_content = re.search(r'<div[^>]*class="rich_media_content[^"]*"[^>]*>(.*?)</div>\s*<div class="rich_media_tool', r.text, re.DOTALL)

        if not m_content:
            print(f"  [警告] 未找到正文内容: 《{title}》")
            return False

        text = clean_wechat_html(m_content.group(1))
        md_content = f"# {title}\n\n"
        md_content += f"- **作者**: 历史第一狗仔队 (honghuidian1230)\n"
        md_content += f"- **发布日期**: {date_str}\n"
        md_content += f"- **原文链接**: {url}\n\n---\n\n"
        md_content += text

        out_file.write_text(md_content, encoding="utf-8")
        print(f"  [√ 成功入库] 《{title}》 ({len(text)} 字) -> {out_file.name}")
        user32.MessageBeep(0)
        return True
    except Exception as e:
        print(f"  [异常] {e}")
        return False

def mode_auto_scroll():
    print("\n=======================================================")
    print("  【全自动滚屏扫荡模式】已准备就绪！")
    print("=======================================================")
    print("请按照以下步骤操作:")
    print("  1. 倒计时期间，请用鼠标【点击一下】微信的“全部消息”文章列表窗口")
    print("  2. 脚本将自动模拟翻页并捕获所有文章链接")
    print("-------------------------------------------------------")
    for i in range(5, 0, -1):
        print(f"  >>> 请立即切换并点击微信列表窗口！倒计时 {i} 秒...")
        time.sleep(1)

    print("\n>>> 开始自动滚屏抓取...")
    all_found = set()
    if URLS_LOG.exists():
        all_found = set(URLS_LOG.read_text(encoding="utf-8").splitlines())

    for step in range(1, 16):
        print(f"  [第 {step}/15 次翻页采样]")
        # 1. Select All & Copy
        press_combo(VK_CONTROL, ord('A'))
        time.sleep(0.15)
        press_combo(VK_CONTROL, ord('C'))
        time.sleep(0.3)
        # 2. Extract
        urls = extract_urls_from_clipboard()
        new_in_batch = [u for u in urls if u not in all_found]
        for u in new_in_batch:
            all_found.add(u)
        print(f"    当前累计发现独立文章链接: {len(all_found)} 篇")
        # 3. Page Down
        press_key(VK_NEXT)
        time.sleep(0.8)

    print(f"\n>>> 扫荡结束！共捕获到 {len(all_found)} 篇微信文章链接！")
    with open(URLS_LOG, "w", encoding="utf-8") as f:
        for u in sorted(all_found):
            f.write(u + "\n")

    print(">>> 正在启动全量下载入库管道...")
    success_count = 0
    for idx, u in enumerate(sorted(all_found), 1):
        print(f"[{idx}/{len(all_found)}] 正在抓取: {u}")
        if download_and_save(u):
            success_count += 1
        time.sleep(0.5)

    print(f"\n全部完成！本次成功入库: {success_count} 篇！")

def mode_clipboard_watcher():
    print("\n=======================================================")
    print("  【剪贴板实时监听模式】已启动！")
    print("=======================================================")
    print("使用说明:")
    print("  在微信电脑版里，无论是点开文章【复制链接】，还是右键【复制链接】，")
    print("  只要剪贴板出现新微信链接，脚本就会立刻“叮”一声自动下载入库！")
    print("  随时按 Ctrl + C 结束。\n")

    seen_urls = set()
    if URLS_LOG.exists():
        seen_urls = set(URLS_LOG.read_text(encoding="utf-8").splitlines())

    while True:
        try:
            urls = extract_urls_from_clipboard()
            for u in urls:
                if u not in seen_urls:
                    print(f"\n[新链接捕获] {u}")
                    ok = download_and_save(u)
                    if ok:
                        seen_urls.add(u)
                        with open(URLS_LOG, "a", encoding="utf-8") as f:
                            f.write(u + "\n")
            time.sleep(0.4)
        except KeyboardInterrupt:
            print("\n监听已停止。")
            break
        except Exception as e:
            time.sleep(1)

if __name__ == "__main__":
    print("=" * 60)
    print("  历史第一狗仔队 - 微信文章全自动采集入库器")
    print("=" * 60)
    print("请选择工作模式:")
    print("  [1] 全自动滚屏扫荡模式 (自动按键翻页提取，20秒搞定全部)")
    print("  [2] 后台剪贴板监听模式 (随复制随存，静默自动下载)")
    print("=" * 60)
    try:
        choice = input("请输入选项 (1 或 2，默认 1): ").strip()
    except Exception:
        choice = "1"
    
    if choice == "2":
        mode_clipboard_watcher()
    else:
        mode_auto_scroll()
