// 桌面壳（Windows）：起后端、等它活过来、把界面交给用户、收摊。
//
// ## 它做什么
//
// 挑一个空端口 → 起后端子进程 → 等它就绪 → **在自己的窗口里显示界面** → 退出时后端也收摊。
//
// ## 为什么是内嵌 WebView2 窗口，而不是让人开浏览器
//
// 和 mac 壳（`Shell.swift` 的 WKWebView）同一个答案：这是**一个软件**，不是一个网页。
// V1 曾经只打印 URL、拉起默认浏览器 —— 理由是"目标机是无头 SSH，窗口画不出也验不了"。
// **那个理由是错的**：2026-09-10 实测，headless SSH 上 in-box csc 编出的 WinForms +
// WebView2 照样把窗口建出来（`EnumWindows` 按 pid 找得到 `title=ScienceMate`），
// WebView2 运行时也报得出版本。判据一直在，只是当初没去找 —— 而代价是同事装完看到
// 的是一个浏览器标签页。
//
// 拿"我验着方便"换掉产品形态，是把纪律用反了：纪律约束的是我的**结论**，不是产品。
// 和 platform/desktop/mac/Shell.swift **同样的四件事、同样的入口**
// （`-m app.launcher start`）：壳里另起一条启动路径的话，两条路会各自演化，
// 而“我在终端里能跑、双击就不行”这类报告没有任何东西能解释。
//
// ## 它不做什么
//
// 不管路由、不管状态、不管权限、不决定数据放哪 —— 那个答案在 app/config.py
// 里只有一份。壳一旦有产品逻辑，同一件事就有了两个答案（一个在网页、一个在
// 壳），分叉时两边都不报错。
//
// ## 谁能让壳退出：窗口关了，或者后端自己没了。就这两件
//
// **stdin 不在这个名单里。** 2026-09-10 真机对照实验：双击起来的壳 2.08 秒就自己
// 退了（exit 0），把同一份源码里监听 stdin EOF 的那几行删掉，同样起法活过 75 秒。
// 原因：`Console.IsInputRedirected` 回答的不是「上游有没有人管着我」——**没有控制台
// 时它同样返回 true**（stdin 句柄是 NULL，不是字符设备），于是 `Console.In.ReadLine()`
// 立刻返回 null，壳把「我没有 stdin」读成了「上游让我收摊」。用它当守卫，恰好在它
// 点名要防的那个场景（双击）里失效。
//
// 所以这里不是把守卫改精细，而是**把这条路整个删掉**：能让壳收摊的信号只剩「窗口
// 被关」和「后端退出」——两件用户看得见的事。想在脚本里停掉壳的人，杀那个 pid 就行
// （打包器自检就是这么做的），不需要产品为夹具留一条只有夹具会走的分支。
//
// ## 没有控制台的进程必须有地方说话
//
// winexe 双击起来没有控制台，`Console.WriteLine` 全部掉进虚空 —— 上面那个 bug 之所以
// 能活到发布前一刻，就是因为它退出时**一个字都留不下**。所以壳一开工先把 stdout/stderr
// 各接一份到 `<数据根>\logs\shell.log`（数据根默认 `%LOCALAPPDATA%\afs`，
// `HARNESS_FRAMEWORK_HOME` 可改，见 `DataRoot()`）；重定向了的调用方照旧从管道读得到。
//
// ## 为什么父死子亡靠 Job Object，而且必须先挂 Job 再放行后端
//
// mac 壳靠后端侧 `--exit-with-parent`（合作式轮询父进程）。Windows 上后端多半
// 由 `uv run` 起 —— 后端的父进程是 uv 的 shim，不是壳；而且合作式那套在壳**崩
// 掉/被强杀**时不成立（没人还能配合）。RFC §11 的答案：壳持一个
// KILL_ON_JOB_CLOSE 的 Job Object，把后端整棵进程树放进去。壳进程一消失
// （正常退出、崩溃、被 taskkill /F 都算），OS 关掉壳持有的最后一个 Job 句柄，
// 连锁杀掉 Job 里的所有进程。这条路 OS 强制执行，不靠任何一方配合。
//
// **顺序即正确性**：不能先 `Process.Start()` 再 `AssignProcessToJobObject()` ——
// 那之间有个窗口，后端若在被并入前就 fork 出子进程，子进程逃出 Job，杀壳时
// 活下来（正是我们要根治的孤儿）。所以走 `CreateProcess(CREATE_SUSPENDED)` →
// 并入 Job → `ResumeThread`：后端跑第一条指令时已在 Job 里，它此后 fork 的一切
// 自动继承 Job。这是架构级的正确，不是"uv 起得慢所以撞不上"的赌。
// `--exit-with-parent` 仍带上，作冗余的第二层。
//
// ## 构建（目标机自带 csc，无需管理员 / VS）
//
//   csc /nologo /target:exe /out:ScienceMate.exe /r:System.Web.Extensions.dll Shell.cs
//   (csc.exe 在 C:\Windows\Microsoft.NET\Framework64\v4.0.30319\；
//    /r:System.Web.Extensions.dll 给 backend.json 用的 JavaScriptSerializer)
//
// 后端如何起来由 exe 旁边的 backend.json 决定（装包时写；开发时手写）：
//   { "executable": "...uv.exe", "arguments": ["run","--python","3.12",
//     "--extra","dev","python","-m","app.launcher","start"],
//     "workingDirectory": "...\\platform\\backend",
//     "environment": { "HARNESS_ROOT": "...\\src" } }
// 壳只往 arguments 追加 --port/--no-browser/--exit-with-parent，别的一律不碰。

using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Runtime.InteropServices;
using System.Text;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;
using System.Threading;

internal static class Shell
{
    // ── Job Object ───────────────────────────────────────────────────────────
    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    private static extern IntPtr CreateJobObjectW(IntPtr a, string name);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool SetInformationJobObject(IntPtr job, int cls, IntPtr info, uint cb);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr proc);

    private const int JobObjectExtendedLimitInformation = 9;
    private const uint JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000;

    [StructLayout(LayoutKind.Sequential)]
    private struct JOBOBJECT_BASIC_LIMIT_INFORMATION
    {
        public long PerProcessUserTimeLimit, PerJobUserTimeLimit;
        public uint LimitFlags;
        public UIntPtr MinimumWorkingSetSize, MaximumWorkingSetSize;
        public uint ActiveProcessLimit;
        public UIntPtr Affinity;
        public uint PriorityClass, SchedulingClass;
    }
    [StructLayout(LayoutKind.Sequential)]
    private struct IO_COUNTERS
    { public ulong a, b, c, d, e, f; }
    [StructLayout(LayoutKind.Sequential)]
    private struct JOBOBJECT_EXTENDED_LIMIT_INFORMATION
    {
        public JOBOBJECT_BASIC_LIMIT_INFORMATION BasicLimitInformation;
        public IO_COUNTERS IoInfo;
        public UIntPtr ProcessMemoryLimit, JobMemoryLimit, PeakProcessMemoryUsed, PeakJobMemoryUsed;
    }

    private static IntPtr CreateKillOnCloseJob()
    {
        IntPtr job = CreateJobObjectW(IntPtr.Zero, null);
        if (job == IntPtr.Zero)
            throw new InvalidOperationException("CreateJobObject failed: " + Marshal.GetLastWin32Error());
        var ext = new JOBOBJECT_EXTENDED_LIMIT_INFORMATION();
        ext.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        int len = Marshal.SizeOf(typeof(JOBOBJECT_EXTENDED_LIMIT_INFORMATION));
        IntPtr buf = Marshal.AllocHGlobal(len);
        try
        {
            Marshal.StructureToPtr(ext, buf, false);
            if (!SetInformationJobObject(job, JobObjectExtendedLimitInformation, buf, (uint)len))
                throw new InvalidOperationException("SetInformationJobObject failed: " + Marshal.GetLastWin32Error());
        }
        finally { Marshal.FreeHGlobal(buf); }
        return job;
    }

    // ── CreateProcess (suspended, redirected to a log file handle) ───────────
    [StructLayout(LayoutKind.Sequential)]
    private struct SECURITY_ATTRIBUTES
    { public int nLength; public IntPtr lpSecurityDescriptor; public int bInheritHandle; }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct STARTUPINFO
    {
        public int cb;
        public string lpReserved, lpDesktop, lpTitle;
        public int dwX, dwY, dwXSize, dwYSize, dwXCountChars, dwYCountChars, dwFillAttribute, dwFlags;
        public short wShowWindow, cbReserved2;
        public IntPtr lpReserved2, hStdInput, hStdOutput, hStdError;
    }
    [StructLayout(LayoutKind.Sequential)]
    private struct PROCESS_INFORMATION
    { public IntPtr hProcess, hThread; public int dwProcessId, dwThreadId; }

    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    private static extern bool CreateProcessW(
        string app, string cmdLine, IntPtr procAttr, IntPtr threadAttr,
        bool inherit, uint flags, IntPtr env, string cwd,
        ref STARTUPINFO si, out PROCESS_INFORMATION pi);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern uint ResumeThread(IntPtr thread);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool GetExitCodeProcess(IntPtr proc, out uint code);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool CloseHandle(IntPtr h);
    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    private static extern IntPtr CreateFileW(
        string name, uint access, uint share, ref SECURITY_ATTRIBUTES sa,
        uint disposition, uint flags, IntPtr template);

    private const uint CREATE_SUSPENDED = 0x00000004;
    private const uint CREATE_NO_WINDOW = 0x08000000;
    private const uint CREATE_UNICODE_ENVIRONMENT = 0x00000400;
    private const int STARTF_USESTDHANDLES = 0x00000100;
    private const uint STILL_ACTIVE = 259;
    /// 后端要求重新拉起时用的退出码。与 app/services/self_update.py 的 RESTART_EXIT_CODE
    /// 是同一个数 —— 壳只认这一个，别的码一律当事故。
    private const uint RESTART_EXIT_CODE = 3;
    private const uint FILE_APPEND_DATA = 0x0004;
    private const uint OPEN_ALWAYS = 4;
    private const uint GENERIC_WRITE = 0x40000000;
    private const uint FILE_SHARE_READ = 0x1, FILE_SHARE_WRITE = 0x2;
    private const uint CREATE_ALWAYS = 2;
    private const uint FILE_ATTRIBUTE_NORMAL = 0x80;

    // ── 后端配置：exe 旁的 backend.json ─────────────────────────────────────
    private sealed class BackendConfig
    {
        public string Executable = "";
        public List<string> Arguments = new List<string>();
        public string WorkingDirectory = "";
        public Dictionary<string, string> Environment = new Dictionary<string, string>();
        /// <summary>这份安装随哪一版来的（打包器写）。空 = 老包，不知道，那就不自替换。</summary>
        public string Version = "";
    }

    private static string ShellDirectory()
    {
        return Path.GetDirectoryName(Process.GetCurrentProcess().MainModule.FileName);
    }

    // backend.json 里的 executable / workingDirectory **相对壳自己所在目录**解析，
    // 不是相对进程 cwd —— 这才是整包能搬走的关键：装包器写相对路径
    // （`Resources\python\python.exe`），壳装到 `%LOCALAPPDATA%\Programs\…` 或被拷到
    // 别处都自己找得到自己的 python。绝对路径（开发时手写）Path.Combine 会原样返回。
    private static string ResolveAgainstShell(string shellDir, string p)
    {
        if (string.IsNullOrEmpty(p)) return p;
        return Path.GetFullPath(Path.Combine(shellDir, p));
    }

    private static BackendConfig LoadConfig(string path)
    {
        string text = File.ReadAllText(path, Encoding.UTF8);
        var ser = new System.Web.Script.Serialization.JavaScriptSerializer();
        var raw = (Dictionary<string, object>)ser.DeserializeObject(text);
        var cfg = new BackendConfig();
        foreach (var kv in raw)
        {
            string key = kv.Key.ToLowerInvariant();
            if (key == "executable") cfg.Executable = Convert.ToString(kv.Value);
            else if (key == "workingdirectory") cfg.WorkingDirectory = Convert.ToString(kv.Value);
            else if (key == "version") cfg.Version = (Convert.ToString(kv.Value) ?? "").Trim();
            else if (key == "arguments" && kv.Value is object[])
                foreach (var a in (object[])kv.Value) cfg.Arguments.Add(Convert.ToString(a));
            else if (key == "environment" && kv.Value is Dictionary<string, object>)
                foreach (var e in (Dictionary<string, object>)kv.Value)
                    cfg.Environment[e.Key] = Convert.ToString(e.Value);
        }
        if (string.IsNullOrEmpty(cfg.Executable))
            throw new InvalidOperationException("backend.json: 'executable' is required");
        return cfg;
    }

    private static int FreePort()
    {
        var listener = new TcpListener(IPAddress.Loopback, 0);
        listener.Start();
        int port = ((IPEndPoint)listener.LocalEndpoint).Port;
        listener.Stop();
        return port;
    }

    // Windows 命令行拼接（CommandLineToArgvW 语义）。
    private static string QuoteArg(string arg)
    {
        if (arg.Length > 0 && arg.IndexOfAny(new[] { ' ', '\t', '"' }) < 0) return arg;
        var sb = new StringBuilder();
        sb.Append('"');
        for (int i = 0; i < arg.Length; i++)
        {
            int bs = 0;
            while (i < arg.Length && arg[i] == '\\') { bs++; i++; }
            if (i == arg.Length) { sb.Append('\\', bs * 2); break; }
            if (arg[i] == '"') { sb.Append('\\', bs * 2 + 1); sb.Append('"'); }
            else { sb.Append('\\', bs); sb.Append(arg[i]); }
        }
        sb.Append('"');
        return sb.ToString();
    }

    // 双 NUL 结尾的 UTF-16 环境块：父环境 + 覆盖项。
    private static byte[] BuildEnvironmentBlock(Dictionary<string, string> overrides)
    {
        var merged = new SortedDictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        foreach (DictionaryEntry e in Environment.GetEnvironmentVariables())
            merged[(string)e.Key] = Convert.ToString(e.Value);
        foreach (var kv in overrides) merged[kv.Key] = kv.Value;
        var sb = new StringBuilder();
        foreach (var kv in merged) { sb.Append(kv.Key); sb.Append('='); sb.Append(kv.Value); sb.Append('\0'); }
        sb.Append('\0');
        return Encoding.Unicode.GetBytes(sb.ToString());
    }

    private static bool HealthReady(string url)
    {
        try
        {
            var req = (HttpWebRequest)WebRequest.Create(url + "health/ready");
            req.Timeout = 3000; req.Method = "GET";
            using (var resp = (HttpWebResponse)req.GetResponse())
                return (int)resp.StatusCode == 200;
        }
        catch { return false; }
    }

    private static bool StillRunning(IntPtr hProcess)
    {
        uint code;
        return GetExitCodeProcess(hProcess, out code) && code == STILL_ACTIVE;
    }


    // ── 界面：自己的窗口 ─────────────────────────────────────────────────────

    /// 在一个 WebView2 窗口里显示界面，阻塞到窗口关闭 / stop / 后端退出。
    ///
    /// WebView2 用**独立的用户数据目录**（数据根下的 `webview2/`）：不碰用户的 Edge
    /// 配置，卸载删目录就干净。起不来就如实打印原因并退回默认浏览器 —— 那是降级，
    /// 要说出口，不能让人对着一个白窗口猜。
    /// 说给用户听，不只说给日志听。
    ///
    /// winexe 没有控制台：一条只写到 stderr 的失败在图标被双击之后等于静默。凡是
    /// "应用因此开不起来"的原因都走这里。
    private static void Complain(string message)
    {
        Console.Error.WriteLine(message);
        try { MessageBox.Show(message, "ScienceMate", MessageBoxButtons.OK, MessageBoxIcon.Warning); }
        catch (Exception ex) { Console.Error.WriteLine("（连弹窗都失败了：" + ex.Message + "）"); }
    }

    private static void ShowTheInterface(string url, ManualResetEventSlim stop, IntPtr backend)
    {
        Form form = null;
        try
        {
            Application.EnableVisualStyles();
            form = new ShellWindow();
            form.Text = "ScienceMate";
            form.Width = 1280; form.Height = 860;
            form.StartPosition = FormStartPosition.CenterScreen;
            var view = new WebView2();
            view.Dock = DockStyle.Fill;
            form.Controls.Add(view);

            string dataDir = Path.Combine(DataRoot(), "webview2");
            Directory.CreateDirectory(dataDir);

            form.Shown += async (s, e) =>
            {
                try
                {
                    var env = await CoreWebView2Environment.CreateAsync(null, dataDir);
                    await view.EnsureCoreWebView2Async(env);
                    // 这个窗口只显示本机后端那一个源。页面里的外链（论文、仓库、mailto）
                    // 交给系统默认浏览器，别让壳变成一个没有地址栏、没有返回键的浏览器；
                    // 别的协议一律拦下。blob: 只认同源的（前端下载/预览用）。
                    var appOrigin = new Uri(url);
                    view.CoreWebView2.NavigationStarting += (sender, navigation) =>
                    {
                        Uri target;
                        if (!Uri.TryCreate(navigation.Uri, UriKind.Absolute, out target))
                        {
                            navigation.Cancel = true;
                            return;
                        }
                        bool local = target.Scheme == appOrigin.Scheme
                            && target.Host == appOrigin.Host && target.Port == appOrigin.Port;
                        bool localBlob = target.Scheme == "blob"
                            && navigation.Uri.StartsWith("blob:" + appOrigin.GetLeftPart(UriPartial.Authority) + "/", StringComparison.Ordinal);
                        if (local || localBlob) return;
                        navigation.Cancel = true;
                        if (target.Scheme == "https" || target.Scheme == "http" || target.Scheme == "mailto")
                            OpenInBrowser(navigation.Uri);
                    };
                    view.CoreWebView2.NewWindowRequested += (sender, request) =>
                    {
                        request.Handled = true;
                        Uri target;
                        if (Uri.TryCreate(request.Uri, UriKind.Absolute, out target)
                            && (target.Scheme == "https" || target.Scheme == "http" || target.Scheme == "mailto"))
                            OpenInBrowser(request.Uri);
                    };
                    view.CoreWebView2.Navigate(url);
                    Console.WriteLine("WEBVIEW READY " + view.CoreWebView2.Environment.BrowserVersionString);
                }
                catch (Exception ex)
                {
                    Console.Error.WriteLine("WEBVIEW FAILED " + ex.GetType().Name + ": " + ex.Message);
                    OpenInBrowser(url);
                }
            };

            // 后端自己退了 / 收到 stop 信号，就把窗口关掉，让消息循环退出。
            // 全限定：本文件也 using 了 System.Threading，裸 `Timer` 有歧义。
            // 「后端退了所以关窗」和「人关了窗」必须分开：前者不能置 stop —— 否则后端为
            // 装更新退的 3 会被当成「人要退出」，应用就此消失，更新要等下次手动启动才生效
            // （真机上 /update/restart 就是这么把窗口关没的）。
            bool backendGone = false;
            var watch = new System.Windows.Forms.Timer();
            watch.Interval = 250;
            watch.Tick += (s, e) =>
            {
                if (!StillRunning(backend)) { backendGone = true; watch.Stop(); form.Close(); }
                else if (stop.IsSet) { watch.Stop(); form.Close(); }
            };
            watch.Start();

            form.FormClosed += (s, e) => { if (!backendGone) stop.Set(); };   // 人关窗口 = 退出应用
            Application.Run(form);
        }
        catch (Exception ex)
        {
            // 连窗口都建不出来（无交互桌面等）：退回 V1 的行为，并说清楚。
            Console.Error.WriteLine("WINDOW FAILED " + ex.GetType().Name + ": " + ex.Message);
            OpenInBrowser(url);
            while (!stop.IsSet && StillRunning(backend)) stop.Wait(250);
        }
    }

    /// <summary>日志目录：`<数据根>\logs`。后端的 backend.log、壳自己的 shell.log 都在这里，
    /// 「打开日志文件夹」打开的也是它 —— 一个答案，三处使用。</summary>
    private static string LogsDirectory()
    {
        return Path.Combine(DataRoot(), "logs");
    }

    /// <summary>在资源管理器里打开日志目录；有 backend.log 就选中它。</summary>
    /// <remarks>
    /// 窗口的系统菜单（标题栏右键 / Alt+空格）和后端出事时的对话框都走这里。界面里的
    /// 「下载诊断包」要后端活着才点得到；后端起不来、中途退出的时候 —— 正是最需要日志
    /// 的时候 —— 只剩壳这一条路。数据根在默认隐藏的 AppData 下，不给路就等于没有日志。
    /// </remarks>
    internal static void OpenLogsFolder()
    {
        try
        {
            string dir = LogsDirectory();
            Directory.CreateDirectory(dir);
            string backendLog = Path.Combine(dir, "backend.log");
            var open = File.Exists(backendLog)
                ? new ProcessStartInfo("explorer.exe", "/select,\"" + backendLog + "\"")
                : new ProcessStartInfo("explorer.exe", "\"" + dir + "\"");
            open.UseShellExecute = true;
            Process.Start(open);
        }
        catch (Exception ex) { Console.Error.WriteLine("打不开日志目录：" + ex.Message); }
    }

    /// <summary>后端出事：说给人听，并问要不要打开日志目录。</summary>
    /// <remarks>
    /// 从前这几处只写 stderr。winexe 没有控制台，那等于双击之后窗口一闪就没了（或者压根
    /// 没出来），一个字都没说 —— 日志明明写着原因，人不知道去哪看。没有交互桌面时（打包器
    /// 经 SSH 自检）MessageBox 会抛，照旧只留日志，和 `Complain` 同一个处理。
    /// </remarks>
    private static void SayItAndOfferTheLogs(string message)
    {
        Console.Error.WriteLine(message);
        try
        {
            var answer = MessageBox.Show(
                message + "\r\n\r\n日志在 " + LogsDirectory() +
                "\r\n找人帮忙时，把里面的 backend.log 一起发过去。\r\n\r\n现在打开日志文件夹吗？",
                "ScienceMate", MessageBoxButtons.YesNo, MessageBoxIcon.Warning);
            if (answer == DialogResult.Yes) OpenLogsFolder();
        }
        catch (Exception ex) { Console.Error.WriteLine("（连弹窗都失败了：" + ex.Message + "）"); }
    }

    private static void OpenInBrowser(string url)
    {
        try { var o = new ProcessStartInfo(url); o.UseShellExecute = true; Process.Start(o); }
        catch (Exception ex)
        {
            // 无交互桌面时打不开是常态（降级路上 URL 已打印）；外链打不开时至少让日志说一句。
            Console.Error.WriteLine("无法打开外部链接 " + url + "：" + ex.Message);
        }
    }

    /// <summary>把 stdout/stderr 各多接一份到 shell.log。</summary>
    /// <remarks>
    /// winexe 双击起来时没有控制台，`Console.Write*` 写进虚空。那意味着壳出任何事都
    /// **一个字都留不下** —— 2026-09-10 那个「双击 2 秒自己退」的 bug 能活到发布前
    /// 一刻，靠的就是这份沉默。日志和后端日志放一起（`%LOCALAPPDATA%\afs\logs\`），
    /// 因为出事的人只会被告诉一个目录。
    ///
    /// 追加而不是覆盖：上一次启动为什么失败，往往要和这一次对着看。调用方重定向了
    /// stdout 时两边都收得到 —— tee 不替换原来那份。
    /// </remarks>
    private static void AlsoLogToAFile()
    {
        try
        {
            string dir = LogsDirectory();
            Directory.CreateDirectory(dir);
            var file = new StreamWriter(
                new FileStream(Path.Combine(dir, "shell.log"), FileMode.Append,
                               FileAccess.Write, FileShare.ReadWrite), new UTF8Encoding(false))
            { AutoFlush = true };
            file.WriteLine("── " + DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + " pid=" +
                           Process.GetCurrentProcess().Id + " ──");
            Console.SetOut(new Tee(Console.Out, file));
            Console.SetError(new Tee(Console.Error, file));
        }
        catch { /* 日志建不起来不该拦住应用启动 */ }
    }

    private sealed class Tee : TextWriter
    {
        private readonly TextWriter _a, _b;
        public Tee(TextWriter a, TextWriter b) { _a = a; _b = b; }
        public override Encoding Encoding { get { return Encoding.UTF8; } }
        public override void Write(char c) { Safe(_a, c); Safe(_b, c); }
        public override void Write(string s) { Safe(_a, s); Safe(_b, s); }
        public override void WriteLine(string s) { SafeLine(_a, s); SafeLine(_b, s); }
        public override void Flush() { try { _a.Flush(); } catch { } try { _b.Flush(); } catch { } }
        private static void Safe(TextWriter w, char c) { try { w.Write(c); } catch { } }
        private static void Safe(TextWriter w, string s) { try { w.Write(s); } catch { } }
        private static void SafeLine(TextWriter w, string s) { try { w.WriteLine(s); } catch { } }
    }

    /// <summary>把「装着的壳是哪一份」写给后端看：`<数据根>\shell.json`（见 `DataRoot()`）。</summary>
    /// <remarks>
    /// 自更新只换 harness 与界面，换不了壳（#953）。后端要回答「这次更新含不含壳的改动、
    /// 装着的壳会不会自己换」，就得知道**装着的壳到底是哪一份** —— 而只有壳自己最清楚：
    /// 它的路径、它的字节、它有没有自替换能力。所以壳一开工先自报家门，后端只读不猜。
    ///
    /// `self_replace` 是 true：这一版的壳会在启动时看数据根里有没有比自己新的壳并换成它
    /// （`ReplaceMyselfIfANewerShellIsStaged`）。后端据此知道壳的改动可以走自更新、不必让人重装。
    ///
    /// 写不出来不拦启动：这只是一句自我介绍，缺了后端按「不知道」处理。
    /// </remarks>
    private static void DeclareMyself()
    {
        try
        {
            string self = Process.GetCurrentProcess().MainModule.FileName;
            string digest;
            using (var sha = System.Security.Cryptography.SHA256.Create())
            using (var fs = File.OpenRead(self))
                digest = BitConverter.ToString(sha.ComputeHash(fs)).Replace("-", "").ToLowerInvariant();
            string dir = DataRoot();
            Directory.CreateDirectory(dir);
            string json = "{\n"
                + "  \"platform\": \"windows\",\n"
                + "  \"path\": " + JsonString(self) + ",\n"
                + "  \"sha256\": \"" + digest + "\",\n"
                + "  \"size\": " + new FileInfo(self).Length + ",\n"
                + "  \"self_replace\": true,\n"
                + "  \"declared_at\": \"" + DateTime.UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ") + "\"\n"
                + "}\n";
            // 先写临时文件再改名：后端可能正好在读，别让它读到半个 JSON。
            string target = Path.Combine(dir, "shell.json");
            string tmp = target + ".tmp";
            File.WriteAllText(tmp, json, new UTF8Encoding(false));
            if (File.Exists(target)) File.Delete(target);
            File.Move(tmp, target);
        }
        catch (Exception ex) { Console.Error.WriteLine("自报家门失败（不影响启动）：" + ex.Message); }
    }

    private static string JsonString(string s)
    {
        var sb = new StringBuilder("\"");
        foreach (char c in s)
        {
            if (c == '"' || c == '\\') sb.Append('\\').Append(c);
            else if (c < ' ') sb.Append("\\u").Append(((int)c).ToString("x4"));
            else sb.Append(c);
        }
        return sb.Append('"').ToString();
    }

    // ── 壳自替换（#953 ④）────────────────────────────────────────────────────
    //
    // 自更新把新壳放在 `<数据根>\payload\<版本>\extras\shell\windows\`（后端启动时把暂存
    // 搬过去的）。壳自己是唯一能换自己的人：运行中的 exe 在 Windows 上**不能覆盖，但能改名**
    // —— 所以：把自己（和旁边的 DLL）改名成 .old，把新的拷上来，拉起新的自己，退出。
    // 和 #951 安装器「装到旁边再换」同一个形状：任何一步失败都落在「旧的还能用」。
    //
    // 两道闸，缺一不可：
    //   1. **版本**：载荷版本必须比 backend.json 里「我随哪一版装的」新。只比哈希会把
    //      「重装了新包、数据根里还留着旧载荷」变成把新壳换成旧的 —— #958 那个坑的壳版。
    //   2. **哈希**：新壳和我不是同一份字节才换。这是循环的终止条件：换完起来的新壳看到
    //      载荷里的壳就是自己，停。
    private const string SHELL_EXE = "ScienceMate.exe";
    private static readonly string[] SHELL_FILES = {
        SHELL_EXE, "Microsoft.Web.WebView2.Core.dll", "Microsoft.Web.WebView2.WinForms.dll", "WebView2Loader.dll" };

    /// <summary>数据根：和 app/config.py 那条规则一样 —— `HARNESS_FRAMEWORK_HOME` 指哪算哪，
    /// 没设就是 `%LOCALAPPDATA%\afs`。日志、WebView2 用户数据、shell.json、自替换要看的
    /// 载荷目录全在这一个根下；打包器自检给它一个临时根，就不会碰这台机器真用着的数据。</summary>
    private static string DataRoot()
    {
        string configured = Environment.GetEnvironmentVariable("HARNESS_FRAMEWORK_HOME");
        return string.IsNullOrWhiteSpace(configured)
            ? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "afs")
            : Path.GetFullPath(configured);
    }

    private static string Sha256Of(string path)
    {
        using (var sha = System.Security.Cryptography.SHA256.Create())
        using (var fs = File.OpenRead(path))
            return BitConverter.ToString(sha.ComputeHash(fs)).Replace("-", "").ToLowerInvariant();
    }

    /// <summary>"0.4.6" → [0,4,6]；和后端 self_update.parse_version 同一把尺（数字段，非数字当 0）。</summary>
    private static int[] ParseVersion(string text)
    {
        string head = (text ?? "").Trim().Split('-')[0].Split('+')[0];
        var parts = new List<int>();
        foreach (var piece in head.Split('.'))
        {
            int n = 0; string digits = "";
            foreach (char c in piece) { if (char.IsDigit(c)) digits += c; else break; }
            int.TryParse(digits, out n); parts.Add(n);
        }
        while (parts.Count < 3) parts.Add(0);
        return parts.ToArray();
    }

    private static bool IsNewer(string candidate, string installed)
    {
        if (string.IsNullOrEmpty(installed)) return false;   // 不知道自己哪一版 → 不动
        int[] a = ParseVersion(candidate), b = ParseVersion(installed);
        for (int i = 0; i < Math.Max(a.Length, b.Length); i++)
        {
            int x = i < a.Length ? a[i] : 0, y = i < b.Length ? b[i] : 0;
            if (x != y) return x > y;
        }
        return false;
    }

    /// <summary>上一次自替换留下的 .old 收掉。收不掉（上一个进程还没退干净）就下次再收。</summary>
    private static void CleanupOldShellFiles()
    {
        foreach (var name in SHELL_FILES)
        {
            string old = Path.Combine(ShellDirectory(), name + ".old");
            try { if (File.Exists(old)) File.Delete(old); } catch { }
        }
    }

    /// <summary>数据根里有比我新的壳就换成它并重新拉起自己。返回 true = 已拉起新的，调用方该退出。</summary>
    private static bool ReplaceMyselfIfANewerShellIsStaged(BackendConfig cfg, string[] args)
    {
        try
        {
            string pointer = Path.Combine(DataRoot(), "payload", "current.json");
            if (!File.Exists(pointer)) return false;
            var ser = new System.Web.Script.Serialization.JavaScriptSerializer();
            var raw = ser.DeserializeObject(File.ReadAllText(pointer, Encoding.UTF8)) as Dictionary<string, object>;
            string version = raw != null && raw.ContainsKey("version") ? Convert.ToString(raw["version"]) : "";
            if (string.IsNullOrEmpty(version)) return false;
            if (!IsNewer(version, cfg.Version))
            {
                Console.WriteLine("SHELL-SWAP skip: payload " + version + " 不比我（" + cfg.Version + "）新");
                return false;
            }
            string candidateDir = Path.Combine(DataRoot(), "payload", version, "extras", "shell", "windows");
            string candidate = Path.Combine(candidateDir, SHELL_EXE);
            if (!File.Exists(candidate)) return false;   // 这一版没带壳：正常，不是每版都改壳
            string self = Process.GetCurrentProcess().MainModule.FileName;
            if (Sha256Of(candidate) == Sha256Of(self))
            {
                Console.WriteLine("SHELL-SWAP skip: 载荷里的壳就是我");
                return false;
            }

            string dir = ShellDirectory();
            var renamed = new List<string>();
            try
            {
                foreach (var name in SHELL_FILES)
                {
                    string incoming = Path.Combine(candidateDir, name);
                    if (!File.Exists(incoming)) continue;           // 载荷没带这个文件：留着原来的
                    string mine = Path.Combine(dir, name), old = mine + ".old";
                    if (File.Exists(old)) File.Delete(old);
                    if (File.Exists(mine)) { File.Move(mine, old); renamed.Add(name); }
                    File.Copy(incoming, mine);
                }
            }
            catch (Exception swap)
            {
                // 回滚：把改了名的挪回来。任何一步失败都落在「旧的还能用」。
                foreach (var name in renamed)
                {
                    string mine = Path.Combine(dir, name), old = mine + ".old";
                    try { if (File.Exists(mine)) File.Delete(mine); if (File.Exists(old)) File.Move(old, mine); } catch { }
                }
                Console.Error.WriteLine("SHELL-SWAP failed, rolled back: " + swap.GetType().Name + ": " + swap.Message);
                return false;
            }
            Console.WriteLine("SHELL-SWAP done: " + cfg.Version + " → " + version + "，拉起新的自己");
            // .NET Framework 4.x（in-box csc 编的目标）没有 ArgumentList，只有一根字符串。
            var quoted = new List<string>();
            foreach (var a in args) quoted.Add("\"" + a.Replace("\"", "\\\"") + "\"");
            var psi = new ProcessStartInfo(Path.Combine(dir, SHELL_EXE))
            { UseShellExecute = true, WorkingDirectory = dir, Arguments = string.Join(" ", quoted) };
            Process.Start(psi);
            return true;
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine("SHELL-SWAP check failed (ignored): " + ex.Message);
            return false;
        }
    }

    [STAThread]
    private static int Main(string[] args)
    {
        AlsoLogToAFile();
        DeclareMyself();
        string configPath = args.Length > 0 ? args[0] : Path.Combine(ShellDirectory(), "backend.json");
        if (!File.Exists(configPath))
        {
            Console.Error.WriteLine("找不到后端配置：" + configPath);
            Console.Error.WriteLine("在 exe 旁放一个 backend.json（见文件头注释）。");
            return 2;
        }
        BackendConfig cfg;
        try { cfg = LoadConfig(configPath); }
        catch (Exception ex) { Console.Error.WriteLine("读配置失败：" + ex.Message); return 2; }

        // 起后端之前先看数据根里有没有比我新的壳（上次更新留下的）。有就换成它并重新拉起
        // 自己 —— 这是壳能被自更新换掉的唯一时机：此刻还没起后端、没开窗口、没锁任何东西。
        CleanupOldShellFiles();
        if (ReplaceMyselfIfANewerShellIsStaged(cfg, args)) return 0;

        // 本机后端**总是起**。从前这里有个分支：数据根里有 server.json 就不起后端，
        // 窗口整个指向那台组织服务器。于是专业版打开就是别人家的登录页 —— 想在本机
        // 开个项目都不行，而一台服务器上住着好几个组织，登录还得先挑一个
        // （2026-09-22 wangd 逐条否掉）。病根是把「项目住哪」做成了「应用是什么」。
        // 现在：组织是本机后端握着的几条连接，项目出生时挑一个家。壳只管起后端。

        // executable / workingDirectory 相对壳目录解析——整包搬走 / 装到别处都能跑。
        string shellDir = ShellDirectory();
        cfg.Executable = ResolveAgainstShell(shellDir, cfg.Executable);
        if (!string.IsNullOrEmpty(cfg.WorkingDirectory))
            cfg.WorkingDirectory = ResolveAgainstShell(shellDir, cfg.WorkingDirectory);

        // 退出的信号只登记一次（放在循环外——放里面每次重启都会再挂一个处理器）。
        // 名单只有窗口和后端；stdin 不在其中，理由见文件头「谁能让壳退出」。
        var stop = new ManualResetEventSlim(false);
        Console.CancelKeyPress += (s, e) => { e.Cancel = true; stop.Set(); };

        // 起后端、等就绪、守着它 —— 后端以退出码 3 退出就再来一遍（自更新）。
        for (;;)
        {
            int port = FreePort();
            string url = "http://127.0.0.1:" + port + "/";
            string logDir = LogsDirectory();
            Directory.CreateDirectory(logDir);
            string logPath = Path.Combine(logDir, "backend.log");

            // 1) 先建 Job。
            IntPtr job;
            try { job = CreateKillOnCloseJob(); }
            catch (Exception ex) { Console.Error.WriteLine(ex.Message); return 3; }

            // 2) 打开可继承的日志句柄，作后端 stdout/stderr。
            var sa = new SECURITY_ATTRIBUTES();
            sa.nLength = Marshal.SizeOf(typeof(SECURITY_ATTRIBUTES));
            sa.bInheritHandle = 1;
            // 追加（FILE_APPEND_DATA + OPEN_ALWAYS），不截断：自更新会让后端退出再拉起，
            // 截断的话刚退出的那个后端的日志 —— 正是更新出问题时要看的那份 —— 就没了。
            IntPtr hLog = CreateFileW(logPath, FILE_APPEND_DATA, FILE_SHARE_READ | FILE_SHARE_WRITE,
                ref sa, OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, IntPtr.Zero);
            if (hLog == new IntPtr(-1))
            { Console.Error.WriteLine("建日志失败：" + Marshal.GetLastWin32Error()); return 3; }

            // 3) 拼命令行 + 环境块。
            var argList = new List<string>(cfg.Arguments);
            argList.Add("--port"); argList.Add(port.ToString());
            argList.Add("--no-browser"); argList.Add("--exit-with-parent");
            var cmd = new StringBuilder();
            cmd.Append(QuoteArg(cfg.Executable));
            foreach (var a in argList) { cmd.Append(' '); cmd.Append(QuoteArg(a)); }

            var envOverrides = new Dictionary<string, string>(cfg.Environment);
            envOverrides["PYTHONUNBUFFERED"] = "1";
            envOverrides["PYTHONDONTWRITEBYTECODE"] = "1";
            byte[] envBlock = BuildEnvironmentBlock(envOverrides);
            IntPtr envPtr = Marshal.AllocHGlobal(envBlock.Length);
            Marshal.Copy(envBlock, 0, envPtr, envBlock.Length);

            var si = new STARTUPINFO();
            si.cb = Marshal.SizeOf(typeof(STARTUPINFO));
            si.dwFlags = STARTF_USESTDHANDLES;
            si.hStdOutput = hLog; si.hStdError = hLog; si.hStdInput = IntPtr.Zero;
            PROCESS_INFORMATION pi;

            // 4) CREATE_SUSPENDED：后端还没跑就先站住。
            bool ok = CreateProcessW(
                null, cmd.ToString(), IntPtr.Zero, IntPtr.Zero, true,
                CREATE_SUSPENDED | CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT,
                envPtr, string.IsNullOrEmpty(cfg.WorkingDirectory) ? null : cfg.WorkingDirectory,
                ref si, out pi);
            Marshal.FreeHGlobal(envPtr);
            if (!ok)
            {
                Console.Error.WriteLine("起后端失败(CreateProcess)：" + Marshal.GetLastWin32Error());
                CloseHandle(hLog);
                return 3;
            }

            // 5) 先并入 Job，再放行 —— 这一步之前后端一条指令都没跑，无逃逸窗口。
            if (!AssignProcessToJobObject(job, pi.hProcess))
                Console.Error.WriteLine("警告：AssignProcessToJobObject 失败(" +
                    Marshal.GetLastWin32Error() + ")，父死子亡退回 --exit-with-parent。");
            ResumeThread(pi.hThread);
            CloseHandle(pi.hThread);
            CloseHandle(hLog); // 后端已持有自己的副本。

            // 6) 等 /health/ready；后端中途退出或始终不就绪都把日志给人看。
            var deadline = DateTime.UtcNow.AddSeconds(90);
            bool ready = false;
            while (DateTime.UtcNow < deadline)
            {
                if (!StillRunning(pi.hProcess))
                {
                    uint code; GetExitCodeProcess(pi.hProcess, out code);
                    SayItAndOfferTheLogs("后端没起来就退出了（code " + code + "）。");
                    return 4;
                }
                if (HealthReady(url)) { ready = true; break; }
                Thread.Sleep(300);
            }
            if (!ready) { SayItAndOfferTheLogs("后端 90 秒内没有准备好。"); return 5; }

            // READY 仍然打到 stdout：装完自检靠它认出后端起来了。winexe 没有控制台，
            // 但调用方重定向了 stdout 时这一行照样送得到（打包器自检就是这么读的）。
            Console.WriteLine("READY " + url);

            // 后端刚起来就把暂存的更新搬成了当前载荷（apply_staged_at_launch）—— 如果那一版
            // 带了新壳，此刻数据根里已经有它了。换成它并重新拉起自己（Job 随之收掉这个后端，
            // 新壳会再起一个）。用户看到的是「窗口会自动回来」，和更新流程里说的一样。
            if (ReplaceMyselfIfANewerShellIsStaged(cfg, args)) return 0;

            // 7) 开自己的窗口显示界面，阻塞到「窗口被关 / Ctrl+C / 后端自己退出」。
            //    WebView2 起不来（没运行时之类）就如实退回浏览器 —— 降级要说出来，不装作正常。
            ShowTheInterface(url, stop, pi.hProcess);
            if (stop.IsSet)
            {
                // 退出即让 Job 句柄随进程关闭 → 连锁杀后端。
                Console.WriteLine("STOPPING");
                return 0;
            }
            // 后端自己退了。码 3 = 「装好了更新，请重新拉起我」—— 重挑端口、重起一遍，
            // 走的就是上面同一条路。别的码 = 事故，把日志位置给人看。
            uint exitCode; GetExitCodeProcess(pi.hProcess, out exitCode);
            CloseHandle(pi.hProcess); CloseHandle(job);
            if (exitCode == RESTART_EXIT_CODE)
            {
                Console.WriteLine("RESTARTING (backend exit code 3)");
                continue;
            }
            SayItAndOfferTheLogs("后端意外退出了（code " + exitCode + "）。");
            return 4;
        }
    }
}

/// <summary>壳的主窗口：一个 Form，系统菜单里多一项「打开日志文件夹」。</summary>
/// <remarks>
/// 系统菜单（标题栏右键 / Alt+空格）是 Windows 上不依赖页面的那一层：界面卡住、后端没
/// 响应的时候它照样在。Mac 壳对应的是菜单栏「帮助 → 打开日志文件夹」。
/// </remarks>
internal sealed class ShellWindow : Form
{
    private const int WM_SYSCOMMAND = 0x0112;
    private const int MF_STRING = 0x0000;
    private const int MF_SEPARATOR = 0x0800;
    // 系统命令 id 的低 4 位留给系统用，自定义的必须是 16 的倍数且小于 0xF000。
    private const int SC_OPEN_LOGS = 0x1F10;

    [DllImport("user32.dll")]
    private static extern IntPtr GetSystemMenu(IntPtr hWnd, bool bRevert);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern bool AppendMenuW(IntPtr hMenu, int uFlags, IntPtr uIDNewItem, string lpNewItem);

    protected override void OnHandleCreated(EventArgs e)
    {
        base.OnHandleCreated(e);
        IntPtr menu = GetSystemMenu(Handle, false);
        if (menu == IntPtr.Zero) return;
        AppendMenuW(menu, MF_SEPARATOR, IntPtr.Zero, null);
        AppendMenuW(menu, MF_STRING, new IntPtr(SC_OPEN_LOGS), "打开日志文件夹");
    }

    protected override void WndProc(ref Message m)
    {
        if (m.Msg == WM_SYSCOMMAND && (m.WParam.ToInt64() & 0xFFF0) == SC_OPEN_LOGS)
        {
            Shell.OpenLogsFolder();
            return;
        }
        base.WndProc(ref m);
    }
}
