// 自解压安装器（Windows）：一个 exe，双击就出一个向导，装到你选的目录并起来。
//
// ## 为什么自己写、不用 7-Zip SFX
//
// 要「装到一个持久目录 + 起应用」的 SFX 得用 `7zSD.sfx`（带 InstallPath 的 GUI 模块），
// 而它**已经不在 7-Zip 官方 extra 包里**（2301 extra 只有 7za.exe，没有任何 .sfx）；
// 官方主安装包里的 `7z.sfx`/`7zCon.sfx` 只会解到临时目录跑完就删，不是持久安装。
// 与其去第三方淘 7zSD.sfx（来源不可信、还得对版本），不如自带一个桩——它只用
// **in-box csc + .NET 自带的 System.IO.Compression / WinForms**（跟壳 #868 同一套
// 工具链），零外部依赖、来源自己控。
//
// ## 双击出的是窗口，不是黑框
//
// 2026-09-21 之前这个桩是 `/target:exe` 的控制台程序：双击弹一个命令行窗口，把包
// 解到 `%LOCALAPPDATA%\Programs\ScienceMate`（资源管理器里默认看不见的目录）就完事 ——
// 装到哪儿不能选、桌面和开始菜单什么都不留。用户装完找不到自己刚装的东西，只能回到
// 那个黑框里看它打印的路径。所以现在：`/target:winexe` + WinForms 向导（`InstallWizard.cs`），
// 三页 —— 选位置和快捷方式、看进度、完成。命令行那条路**原样留着**（`--no-launch`
// 等旗标），打包器的装完自检走的就是它，不需要有人坐在屏幕前点。
//
// ## 静默模式下快捷方式默认不建
//
// GUI 里两个复选框默认都是勾上的（装完桌面上有图标，这才是用户要的）。但命令行那条路
// **默认一个快捷方式都不建、也不注册「应用和功能」** —— 因为它唯一的调用方是打包自检，
// 而自检装的是一个隔离的临时目录，它绝不该往打包机真实的桌面和注册表上写东西。要在
// 静默模式下建，就得显式说（`--desktop-shortcut` / `--start-menu` / `--register-uninstall`），
// 而自检显式说的时候会连同 `--desktop-dir` / `--start-menu-dir` / `--uninstall-key`
// 一起把落点改到隔离目录里去 —— 这样「建快捷方式」这条路真被跑过，又没碰打包机。
//
// ## 单文件怎么装下 857MB
//
// 打包器把它拼成：`stub.exe  ‖  payload.zip  ‖  对齐填充  ‖  footer(16B)`。footer = 小端
// `Int64 payloadOffset`（= stub.exe 长度）+ `Int64 MAGIC`。运行时读自己这个文件：
// 先按 PE 头找 Authenticode 证书表 —— 签名会把证书表**追加**在文件末尾，签过之后
// EOF 就不再是 footer 的位置；证书表在就以它的起点为「载荷末尾」，不在才用 EOF
// （`PayloadEnd`）。末 16B 拿到 offset，把 [offset, end-16) 那段（zip + 填充）拷到临时
// 文件，逐个成员解到 staging（`ExtractPackage`：拒绝绝对路径、盘符/ADS、`..`，只许
// `CreateNew`），删临时 zip，起 `ScienceMate.exe`。zip 的中央目录偏移是相对 zip 自身
// 起点的，所以先拷成独立 zip 再解最省心（不用去凑「带前缀的 zip」那套偏移）。
//
// ## 构建
//
//   csc /nologo /target:winexe /out:InstallerStub.exe \
//       /r:System.IO.Compression.FileSystem.dll /r:System.IO.Compression.dll \
//       /r:System.Windows.Forms.dll /r:System.Drawing.dll /r:System.Web.Extensions.dll \
//       InstallerStub.cs InstallWizard.cs InstallFootprint.cs
//   然后打包器把 payload.zip + footer 追加到 InstallerStub.exe 上，得到 Setup.exe。
//
// ## 为什么「装到旁边再换」，而不是「先删旧的再解」
//
// 2026-09-10 真机：应用开着的时候重跑 Setup.exe（**这正是升级要走的路** —— 自更新
// 只换 harness 与界面，壳变了必须重装），`Directory.Delete(installDir, true)` 删到
// 一半撞上被占用的 `Microsoft.Web.WebView2.Core.dll` 抛
// `UnauthorizedAccessException`，于是留下**一个坏掉的安装**：`Resources\tectonic`
// 已经没了，应用还能起来，但产不出 PDF —— 而用户完全不知道为什么。
//
// 「先删后装」这个写法的问题不是不够小心，是**顺序本身**：它在还没有能用的新东西
// 之前，先把唯一能用的旧东西拆了。任何一步失败都落在「两个都没有」上。
//
// 所以：解到 `<installDir>.new-<事务id>` → 验一眼该有的东西都在 → 才把旧的挪成
// `.old-<事务id>`、新的换上去（换不上去就把旧的挪回来）→ 删 `.old-…`（删不掉也无所谓，
// 新安装已经就位）。失败一律落在「旧的原封不动」；每次安装各用一个事务 id，两个安装器
// 撞在一起也不会互相清掉对方的 staging。
//
// 另外**动手之前先看有没有实例在跑**：不看的话，即便顺序对了，`Directory.Move` 也会
// 被文件锁挡住；而且那时用户已经等了半分钟解压。发现了就明说、什么都不动。
// 同一个安装目录同时只许一个安装器动手（按目录取名的 Mutex）；目标目录里有别人的
// 文件（既不是空目录也不是一份 ScienceMate 安装）就拒绝 —— 别把人家的目录当 staging。
// `--install-dir <绝对路径>` 改安装位置（向导里是「浏览…」那个按钮）。

using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Threading;

/// <summary>装哪儿、装完留下什么。向导填它，命令行也填它，往下只有这一份答案。</summary>
internal sealed class InstallOptions
{
    public string InstallDir;
    public bool Launch = true;
    /// <summary>无 GUI（命令行/自检）。</summary>
    public bool Silent;
    public bool DesktopShortcut;
    public bool StartMenuShortcut;
    public bool RegisterUninstall;
    /// <summary>自检用：把落点改到隔离目录，别碰打包机真实的桌面/开始菜单/注册表。</summary>
    public string DesktopDirOverride;
    public string StartMenuDirOverride;
    public string UninstallKey = Footprint.DefaultUninstallKey;

    /// <summary>人双击进来时该有的默认值 —— 桌面图标、开始菜单、能卸载。</summary>
    /// <remarks>
    /// 命令行那条路一个都不默认开（它唯一的调用方是打包自检，而自检装的是隔离目录，
    /// 绝不该往打包机真实的桌面和注册表上写东西）。所以「默认开」这件事只属于人，
    /// 就写在这里。
    /// </remarks>
    public InstallOptions ForAPerson()
    {
        DesktopShortcut = true;
        StartMenuShortcut = true;
        RegisterUninstall = true;
        return this;
    }

    public static string DefaultInstallDir()
    {
        return Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            "Programs", Footprint.ProductName);
    }
}

/// <summary>安装没做成，且已经说清了是为什么。<see cref="Code"/> 是进程退出码。</summary>
internal sealed class InstallRefused : Exception
{
    public readonly int Code;
    public InstallRefused(string message, int code) : base(message) { Code = code; }
}

/// <summary>装完了 —— 以及顺带没做成的那些不致命的事。</summary>
internal sealed class InstallOutcome
{
    public string ExePath;
    public string InstallDir;
    /// <summary>快捷方式没建成之类：不影响「装好了」，但得让人知道。</summary>
    public readonly List<string> Warnings = new List<string>();
}

internal static class Installer
{
    /// <summary>出错要让人看见。</summary>
    /// <remarks>
    /// 双击起来的程序，退出的那一刻窗口就关 —— 一段 .NET 栈闪一下没了，和什么都没说
    /// 一样（上面那个坏安装就是这么无声无息的）。所以错误既写 stderr（脚本读得到）
    /// 又弹一个框（人看得到）。命令行模式不弹框 —— 脚本靠退出码和 stderr，弹框只会把它挂住。
    /// </remarks>
    private static int Fail(string message, int code, bool interactive)
    {
        Console.Error.WriteLine(message);
        if (interactive)
        {
            try { Footprint.MessageBoxW(IntPtr.Zero, message, "ScienceMate 安装", Footprint.IconError); }
            catch { /* 弹不出来也不能改变退出码 */ }
        }
        return code;
    }

    // Extended paths let System.IO reach deep package members independently
    // of machine-wide LongPathsEnabled policy. Keep display/launch paths normal.
    private static string IoPath(string path)
    {
        return Footprint.IoPath(path);
    }

    /// <summary>删掉一棵不属于任何人的残骸目录；删不掉就说清楚。</summary>
    private static bool Nuke(string dir)
    {
        dir = IoPath(dir);
        if (!Directory.Exists(dir)) return true;
        try { Directory.Delete(dir, true); return true; }
        catch { return false; }
    }

    /// <summary>这个安装位置里的 ScienceMate 有没有正在跑的。</summary>
    /// <remarks>
    /// 按**这一份安装的 exe 路径**认，不按进程名认：同名进程可能是别处的另一份安装，
    /// 甚至是别的用户的 —— 拿它当理由拒绝安装是误报。
    /// </remarks>
    private static int RunningPidIn(string installDir)
    {
        string exe = Path.Combine(installDir, Footprint.ShellExeName);
        foreach (Process p in Process.GetProcessesByName("ScienceMate"))
        {
            try
            {
                if (string.Equals(p.MainModule.FileName, exe, StringComparison.OrdinalIgnoreCase))
                    return p.Id;
            }
            catch { /* 拿不到 MainModule（权限/已退出）就当不是它 */ }
        }
        return 0;
    }
    // "AFS_ZIP1"（小端 Int64）。footer 魔数，用来确认这是真拼过 payload 的安装包、
    // 不是光一个桩。
    private const long MAGIC = 0x3150495A5F534641L;

    // The PE certificate directory is a file offset, not an RVA. Authenticode
    // appends certificates after our aligned payload; EOF no longer locates it.
    // https://learn.microsoft.com/en-us/windows/win32/debug/pe-format
    private static long PayloadEnd(FileStream file)
    {
        var reader = new BinaryReader(file);
        file.Position = 0x3c;
        long pe = reader.ReadUInt32();
        if (pe < 64 || pe + 24 + 152 > file.Length)
            throw new InvalidDataException("Invalid PE header");
        file.Position = pe;
        if (reader.ReadUInt32() != 0x00004550)
            throw new InvalidDataException("Invalid PE signature");
        file.Position = pe + 24;
        ushort format = reader.ReadUInt16();
        if (format != 0x10b && format != 0x20b)
            throw new InvalidDataException("Unsupported PE format");
        file.Position = pe + 24 + (format == 0x20b ? 144 : 128);
        long certificate = reader.ReadUInt32();
        long size = reader.ReadUInt32();
        if (certificate == 0 && size == 0) return file.Length;
        if (certificate < pe + 24 + 152 || size < 8 || certificate + size != file.Length)
            throw new InvalidDataException("Invalid certificate table");
        return certificate;
    }

    /// <param name="progress">解到第几个成员了（0..1）。向导拿它推进度条；命令行传 null。</param>
    private static void ExtractPackage(string archivePath, string destination, Action<double> progress)
    {
        string root = Path.GetFullPath(destination).TrimEnd('\\') + "\\";
        using (var archive = ZipFile.OpenRead(archivePath))
        {
            long total = 0, done = 0;
            foreach (var entry in archive.Entries) total += entry.Length;
            if (total <= 0) total = 1;
            foreach (var entry in archive.Entries)
            {
                // ZIP uses '/', while extended Win32 paths require '\\'.
                string relative = entry.FullName.Replace('/', '\\');
                if (Path.IsPathRooted(relative) || relative.IndexOf(':') >= 0)
                    throw new InvalidDataException("Invalid package entry: " + entry.FullName);
                string target = Path.GetFullPath(Path.Combine(root, relative));
                if (!target.StartsWith(root, StringComparison.OrdinalIgnoreCase))
                    throw new InvalidDataException("Package entry escapes installation: " + entry.FullName);
                if (relative.EndsWith("\\", StringComparison.Ordinal))
                    Directory.CreateDirectory(IoPath(target));
                else
                {
                    Directory.CreateDirectory(IoPath(Path.GetDirectoryName(target)));
                    using (var source = entry.Open())
                    using (var output = new FileStream(IoPath(target), FileMode.CreateNew, FileAccess.Write, FileShare.None))
                        source.CopyTo(output);
                }
                done += entry.Length;
                if (progress != null) progress(Math.Min(1.0, (double)done / total));
            }
        }
    }

    /// <summary>把目录挪过去，给刚写完的文件一点被放开的时间。</summary>
    /// <remarks>
    /// 2026-09-21 真机：解压完 26070 个文件后紧接着 `Directory.Move` staging，撞上
    /// `UnauthorizedAccessException: Access to the path '…\ScienceMate.new-…' is denied.`
    /// —— 目录是这个安装器一秒钟前自己造的，没有别人该碰它；攥着它的是杀毒软件
    /// （刚落盘的文件正在被实时扫描）。这不是「谁的权限不对」，是**时序**：扫描几十秒
    /// 后自己就放手了。
    ///
    /// 这种失败按运气分布 —— 装到 `%TEMP%` 的打包自检一直没撞上，换个目录第一次就撞上。
    /// 所以不是「重试让它更稳」，是**不重试就等于把安装能不能成交给杀软的时序**。
    ///
    /// 两处 Move 的耐心不一样，因为它们等的是不同的东西：
    /// - staging → installDir：等的是杀软放开**我们刚写的**文件，等得起，给足 60 秒。
    /// - installDir → retired：等的多半是**用户把应用开着**，那么等多久都不会变 ——
    ///   5 秒只是兜住「刚刚才关掉、句柄还没回收」，超过就该明说。
    /// </remarks>
    private static void MoveWithPatience(string from, string to, TimeSpan patience)
    {
        DateTime deadline = DateTime.UtcNow + patience;
        while (true)
        {
            try { Directory.Move(IoPath(from), IoPath(to)); return; }
            catch (Exception)
            {
                if (DateTime.UtcNow >= deadline) throw;
                Thread.Sleep(500);
            }
        }
    }

    /// <summary>目标目录现在能不能装 —— 在**一个字节都还没动**的时候把话说完。</summary>
    /// <remarks>
    /// 向导在用户点「安装」之前就调它一次：位置选得不对，当场在那一页说，而不是等解压
    /// 了半分钟再弹错误框。引擎在真动手前会再调一次（中间用户可能把应用打开了）。
    /// </remarks>
    public static string WhyNotInstallable(string installDir)
    {
        try
        {
            if (string.IsNullOrWhiteSpace(installDir) || !Path.IsPathRooted(installDir))
                return "请填一个绝对路径，例如 C:\\Users\\你\\ScienceMate。";
            string full = Path.GetFullPath(installDir).TrimEnd(Path.DirectorySeparatorChar);
            if (string.IsNullOrEmpty(Path.GetDirectoryName(full)))
                return "不能安装到磁盘根目录。";
            if (Directory.Exists(IoPath(full)) && Directory.GetFileSystemEntries(IoPath(full)).Length > 0
                && !IsAnInstallation(full))
                return "这个目录里已经有别的文件了。请选一个空目录，或已有的 ScienceMate 安装目录。";
            int running = RunningPidIn(full);
            if (running != 0)
                return "ScienceMate 正在运行（pid " + running + "）。请先关掉它再安装。";
            string refused = WhyThisFolderRefusesUs(full);
            if (refused != null) return refused;
            return null;
        }
        catch (Exception ex) { return "这个路径用不了：" + ex.Message; }
    }

    /// <summary>写得进去吗 —— 答法是**真去建一个目录再删掉**，不是看路径长什么样。</summary>
    /// <remarks>
    /// 2026-09-23 真机（wangd）：安装位置被改成 `C:\Program Files\SM\ScienceMate`，
    /// 向导照单全收，一路走到解压才炸，给出的是一句 .NET 原文 ——
    /// 「对路径"\\?\C:\Program Files\SM\ScienceMate.new-25e68b40…"的访问被拒绝」。
    /// 那句话里对用户有意义的东西是零：`\\?\` 是我们自己加的长路径前缀，
    /// `.new-&lt;事务id&gt;` 是我们内部的 staging 名字，而"访问被拒绝"背后真正的事实 ——
    /// **那个位置要管理员权限** —— 一个字都没说。
    ///
    /// 而这件事在他**选完路径那一刻**就答得出来。答案晚到的代价不是多等一会儿：
    /// 他已经点了安装、等过一次，然后拿到一句看不懂的话，还不知道下一步该干什么。
    ///
    /// **为什么不按路径判**（"以 C:\Program Files 开头就拒"）：那是一条会漂的规则。
    /// 提权跑的安装器**写得进去**，于是它会去挡住一次本来能成的安装；而写不进去的地方
    /// 也远不止这一个（`C:\Windows`、别人的用户目录、只读盘、被组策略锁住的目录、
    /// 没权限的网络共享）。真去写一次，答案就不会错 —— 而且它做的正是安装第一步要做
    /// 的那件事（在父目录里建 staging，见 <see cref="Install"/>），所以它答的就是那一问。
    /// </remarks>
    private static string WhyThisFolderRefusesUs(string installDir)
    {
        // 探父目录：安装第一步在那儿建 `<装到哪>.new-<事务id>`。父目录还不存在就
        // 往上找到第一个存在的祖先 —— 那一层才是"建得出来吗"真正被回答的地方。
        string probeIn = Path.GetDirectoryName(installDir);
        while (!string.IsNullOrEmpty(probeIn) && !Directory.Exists(IoPath(probeIn)))
        {
            string up = Path.GetDirectoryName(probeIn);
            if (string.Equals(up, probeIn, StringComparison.Ordinal)) break;
            probeIn = up;
        }
        if (string.IsNullOrEmpty(probeIn) || !Directory.Exists(IoPath(probeIn)))
            return "找不到这个位置所在的盘。换一个路径试试。";

        string probe = Path.Combine(
            probeIn, Footprint.ProductName + ".probe-" + Guid.NewGuid().ToString("N"));
        try
        {
            Directory.CreateDirectory(IoPath(probe));
            Directory.Delete(IoPath(probe));
            return null;
        }
        catch (UnauthorizedAccessException)
        {
            return "装不进这个位置 —— 它要管理员权限。换成 "
                 + InstallOptions.DefaultInstallDir()
                 + "（不用权限），或者右键这个安装器、用「以管理员身份运行」重开一次。";
        }
        catch (IOException ex)
        {
            return "写不进这个位置：" + ex.Message;
        }
        catch (Exception ex)
        {
            return "这个位置用不了：" + ex.Message;
        }
        finally
        {
            // 探针不该留下任何东西。上面 Delete 抛了的话这里兜底，兜不住也不吭声 ——
            // 一个空目录不值得把"能不能装"这个答案搅浑。
            try { if (Directory.Exists(IoPath(probe))) Directory.Delete(IoPath(probe)); }
            catch { }
        }
    }

    private static bool IsAnInstallation(string dir)
    {
        return File.Exists(IoPath(Path.Combine(dir, Footprint.ShellExeName)))
            && File.Exists(IoPath(Path.Combine(dir, "backend.json")));
    }

    /// <summary>真正装。向导在后台线程调它，命令行在主线程调它 —— 同一条路。</summary>
    /// <param name="progress">(这一步在干什么, 0..1 或 -1 表示说不出百分比)</param>
    public static InstallOutcome Install(InstallOptions options, Action<string, double> progress)
    {
        Action<string, double> say = progress ?? delegate { };
        Mutex installLock = null;
        string tmpZip = null;
        string staging = null;
        try
        {
            string self = Process.GetCurrentProcess().MainModule.FileName;
            long total;
            long offset, magic;
            using (var fs = File.OpenRead(self))
            {
                total = PayloadEnd(fs);
                fs.Seek(total - 16, SeekOrigin.Begin);
                var reader = new BinaryReader(fs);
                offset = reader.ReadInt64();
                magic = reader.ReadInt64();
            }
            if (magic != MAGIC || offset < 64 || offset >= total - 16)
                throw new InstallRefused("坏的安装包：footer 魔数不对（没拼上 payload？）", 2);

            string installDir = Path.GetFullPath(options.InstallDir).TrimEnd(Path.DirectorySeparatorChar);
            string lockName;
            using (var sha = SHA256.Create())
                lockName = "Local\\ScienceMate-Install-" + BitConverter.ToString(
                    sha.ComputeHash(Encoding.UTF8.GetBytes(installDir.ToUpperInvariant()))).Replace("-", "");
            installLock = new Mutex(false, lockName);
            bool acquired;
            try { acquired = installLock.WaitOne(0); }
            catch (AbandonedMutexException) { acquired = true; }
            if (!acquired)
            {
                installLock.Dispose(); installLock = null;
                throw new InstallRefused("另一个安装程序正在使用这个目录，请等它完成。", 4);
            }
            if (Directory.Exists(IoPath(installDir))
                && Directory.GetFileSystemEntries(IoPath(installDir)).Length > 0
                && !IsAnInstallation(installDir))
                throw new InstallRefused("目标目录包含其他文件，请选择空目录或已有的 ScienceMate 安装目录。", 5);

            // 动手之前先问「它是不是正开着」——什么都还没动的时候说，代价最小。
            int running = RunningPidIn(installDir);
            if (running != 0)
                throw new InstallRefused("ScienceMate 正在运行（pid " + running + "）。\n\n"
                                         + "请先关掉它，再运行这个安装程序。\n"
                                         + "现在什么都没有改动，已装好的那份完好无损。", 4);

            Console.WriteLine("安装到 " + installDir + " …");
            say("准备安装包…", -1);

            tmpZip = Path.Combine(
                Path.GetTempPath(), "afs-setup-" + Guid.NewGuid().ToString("N") + ".zip");
            using (var src = File.OpenRead(self))
            using (var dst = File.Create(tmpZip))
            {
                src.Seek(offset, SeekOrigin.Begin);
                long remaining = total - 16 - offset;
                long size = remaining;
                var buffer = new byte[1 << 20];
                while (remaining > 0)
                {
                    int n = src.Read(buffer, 0, (int)Math.Min(buffer.Length, remaining));
                    if (n <= 0) break;
                    dst.Write(buffer, 0, n);
                    remaining -= n;
                    say("准备安装包…", size <= 0 ? -1 : (double)(size - remaining) / size * 0.15);
                }
            }

            // 装到旁边，装好了再换。理由见文件头「为什么装到旁边再换」——
            // 在有能用的新东西之前，绝不拆掉唯一能用的旧东西。
            string transaction = Guid.NewGuid().ToString("N");
            staging = installDir + ".new-" + transaction;
            string retired = installDir + ".old-" + transaction;

            Directory.CreateDirectory(IoPath(staging));
            say("正在解压文件…", 0.15);
            ExtractPackage(tmpZip, staging, delegate(double fraction) {
                say("正在解压文件…", 0.15 + fraction * 0.75);
            });
            File.Delete(tmpZip);
            tmpZip = null;

            // 先验新的，再碰旧的。这一句之前失败，旧安装一个字节都没动过。
            string stagedExe = Path.Combine(staging, Footprint.ShellExeName);
            if (!File.Exists(IoPath(stagedExe)))
                throw new InstallRefused("解出来没找到 " + stagedExe + " —— 包不对。\n"
                                         + "已装好的那份没有改动。", 3);

            say("正在就位…", 0.92);
            if (Directory.Exists(IoPath(installDir)))
            {
                try { MoveWithPatience(installDir, retired, TimeSpan.FromSeconds(5)); }
                catch (Exception move)
                {
                    // 多半是刚刚有人把应用打开了（上面查的那一刻还没开）。
                    throw new InstallRefused("换不动旧的安装（" + move.GetType().Name + "：" + move.Message + "）。\n\n"
                                             + "多半是 ScienceMate 刚被打开了 —— 关掉它再装。\n"
                                             + "已装好的那份完好无损。", 6);
                }
            }
            try { MoveWithPatience(staging, installDir, TimeSpan.FromSeconds(60)); }
            catch (Exception move)
            {
                if (Directory.Exists(IoPath(retired)) && !Directory.Exists(IoPath(installDir)))
                    Directory.Move(IoPath(retired), IoPath(installDir));
                throw new InstallRefused(
                    "装好的文件换不到位（" + move.GetType().Name + "：" + move.Message + "）。\n\n"
                    + "有别的程序正攥着刚解压出来的文件（多半是杀毒软件在扫描），等了 60 秒仍没放开。\n"
                    + "过一会儿再装一次即可；" + (Directory.Exists(IoPath(installDir))
                        ? "已装好的那份完好无损。" : "这台机器上还没有装过，什么都没留下。"), 6);
            }
            staging = null;
            if (!Nuke(retired))
                Console.Error.WriteLine("新安装已就位，旧安装清理未完成：" + retired);

            var outcome = new InstallOutcome
            {
                InstallDir = installDir,
                ExePath = Path.Combine(installDir, Footprint.ShellExeName),
            };
            say("正在创建快捷方式…", 0.96);
            LeaveTheFootprint(options, installDir, outcome);

            Console.WriteLine("装好了：" + outcome.ExePath);
            foreach (string warning in outcome.Warnings) Console.Error.WriteLine(warning);
            say("完成", 1.0);
            return outcome;
        }
        finally
        {
            if (installLock != null) { installLock.ReleaseMutex(); installLock.Dispose(); }
            if (tmpZip != null) { try { File.Delete(tmpZip); } catch { } }
            if (staging != null && !Nuke(staging))
                Console.Error.WriteLine("安装临时目录清理未完成：" + staging);
        }
    }

    /// <summary>快捷方式、「应用和功能」那一条、以及记下它们的清单。</summary>
    /// <remarks>
    /// 这一步里**任何一件失败都不让安装失败**：文件已经全部就位、应用已经能用了，
    /// 因为一个图标没建成就把整次安装报成失败，是拿最轻的事去否定最重的事。失败收进
    /// `Warnings`，向导在完成页照原样说出来。
    ///
    /// 清单最后写：它记的是「已经做成了什么」，不是「打算做什么」。
    /// </remarks>
    private static void LeaveTheFootprint(InstallOptions options, string installDir, InstallOutcome outcome)
    {
        string exe = Path.Combine(installDir, Footprint.ShellExeName);
        var record = new InstallRecord
        {
            installDir = installDir,
            version = Footprint.VersionIn(installDir),
            dataRoot = Footprint.DataRoot(),
        };

        if (options.DesktopShortcut)
        {
            string lnk = Path.Combine(Footprint.DesktopDir(options.DesktopDirOverride),
                                      Footprint.ProductName + ".lnk");
            try
            {
                Footprint.CreateShortcut(lnk, exe, installDir, "ScienceMate", null);
                record.shortcuts.Add(lnk);
            }
            catch (Exception ex) { outcome.Warnings.Add("桌面快捷方式没建成：" + ex.Message); }
        }

        if (options.StartMenuShortcut)
        {
            string dir = Footprint.StartMenuDir(options.StartMenuDirOverride);
            string lnk = Path.Combine(dir, Footprint.ProductName + ".lnk");
            try
            {
                Footprint.CreateShortcut(lnk, exe, installDir, "ScienceMate", null);
                record.shortcuts.Add(lnk);
            }
            catch (Exception ex) { outcome.Warnings.Add("开始菜单快捷方式没建成：" + ex.Message); }

            // 卸载入口也放在开始菜单那个文件夹里：从「应用和功能」找得到，从开始菜单
            // 也找得到 —— 装的时候在哪儿看见它，卸的时候就该在哪儿找得到它。
            string uninstaller = Path.Combine(installDir, Footprint.UninstallerExeName);
            if (File.Exists(IoPath(uninstaller)))
            {
                string lnkUninstall = Path.Combine(dir, "卸载 " + Footprint.ProductName + ".lnk");
                try
                {
                    Footprint.CreateShortcut(lnkUninstall, uninstaller, installDir, "卸载 ScienceMate", null);
                    record.shortcuts.Add(lnkUninstall);
                }
                catch (Exception ex) { outcome.Warnings.Add("开始菜单里的卸载项没建成：" + ex.Message); }
            }
            else outcome.Warnings.Add("安装包里没有 " + Footprint.UninstallerExeName + " —— 这一版装完没有卸载入口。");
        }

        if (options.RegisterUninstall)
        {
            try
            {
                Footprint.RegisterUninstallEntry(options.UninstallKey, installDir, record.version,
                                                 Footprint.KilobytesOnDisk(installDir));
                record.uninstallKey = options.UninstallKey;
            }
            catch (Exception ex)
            {
                outcome.Warnings.Add("没能登记到「应用和功能」列表：" + ex.Message);
            }
        }

        try { record.Save(installDir); }
        catch (Exception ex)
        {
            // 清单写不进去，卸载器就只能按默认位置去找快捷方式（它还是会核对指向）。
            outcome.Warnings.Add("安装清单没写成（卸载时将按默认位置查找）：" + ex.Message);
        }
    }


    /// <summary>往 stdout/stderr 说的话一律 UTF-8。</summary>
    /// <remarks>
    /// 写出去的是中文。不摆明编码，.NET 就按机器的 ANSI 代码页编 —— 在非中文区域设置
    /// 的 Windows 上，打包自检按 UTF-8 读回来的是一串 `?`，出了事看不懂它说了什么。
    ///
    /// 这里**不能**用 `Console.OutputEncoding = …`：那个 setter 要一个真的控制台句柄，
    /// 而这是 winexe（双击时根本没有控制台，被重定向时 stdout 是文件句柄），它会抛，
    /// 抛完就静悄悄回落到 ANSI —— 和没写一样。直接把标准流包成 UTF-8 的 writer 就没
    /// 有这个前提。
    /// </remarks>
    private static void SpeakUtf8()
    {
        try
        {
            var encoding = new UTF8Encoding(false);
            Console.SetOut(new StreamWriter(Console.OpenStandardOutput(), encoding) { AutoFlush = true });
            Console.SetError(new StreamWriter(Console.OpenStandardError(), encoding) { AutoFlush = true });
        }
        catch { /* 说不成 UTF-8 也不能因此装不上 */ }
    }

    private const string Usage =
        "用法：Setup.exe [--install-dir 绝对目录] [--no-launch] [--silent]\n"
        + "              [--desktop-shortcut] [--start-menu] [--register-uninstall]\n"
        + "              [--desktop-dir 目录] [--start-menu-dir 目录] [--uninstall-key HKCU子键]\n"
        + "\n不带参数双击 = 图形安装向导。";

    /// <summary>命令行解析。解不动就返回 null 并把话说在 <paramref name="error"/> 里。</summary>
    private static InstallOptions Parse(string[] args, out string error)
    {
        error = null;
        var options = new InstallOptions { InstallDir = InstallOptions.DefaultInstallDir() };
        for (int i = 0; i < args.Length; i++)
        {
            string arg = args[i];
            switch (arg)
            {
                case "--no-launch": options.Launch = false; options.Silent = true; break;
                case "--silent": options.Silent = true; break;
                case "--desktop-shortcut": options.DesktopShortcut = true; break;
                case "--start-menu": options.StartMenuShortcut = true; break;
                case "--register-uninstall": options.RegisterUninstall = true; break;
                case "--install-dir":
                    if (++i >= args.Length || !Path.IsPathRooted(args[i])) { error = Usage; return null; }
                    options.InstallDir = Path.GetFullPath(args[i]).TrimEnd(Path.DirectorySeparatorChar);
                    if (string.IsNullOrEmpty(Path.GetDirectoryName(options.InstallDir)))
                    { error = "不能安装到磁盘根目录"; return null; }
                    break;
                case "--desktop-dir":
                    if (++i >= args.Length || !Path.IsPathRooted(args[i])) { error = Usage; return null; }
                    options.DesktopDirOverride = args[i];
                    break;
                case "--start-menu-dir":
                    if (++i >= args.Length || !Path.IsPathRooted(args[i])) { error = Usage; return null; }
                    options.StartMenuDirOverride = args[i];
                    break;
                case "--uninstall-key":
                    if (++i >= args.Length || string.IsNullOrWhiteSpace(args[i])) { error = Usage; return null; }
                    options.UninstallKey = args[i].Trim('\\');
                    break;
                default:
                    error = "不认识的参数：" + arg + "\n\n" + Usage;
                    return null;
            }
        }
        return options;
    }

    [STAThread]
    private static int Main(string[] args)
    {
        // The in-box compiler otherwise selects legacy .NET path behavior.
        // Opt into native long paths before any System.IO operation; no registry
        // changes or shortened scientific filenames are required.
        AppContext.SetSwitch("Switch.System.IO.UseLegacyPathHandling", false);
        AppContext.SetSwitch("Switch.System.IO.BlockLongPaths", false);
        SpeakUtf8();

        string error;
        InstallOptions options;
        // Path.IsPathRooted / GetFullPath 会对含非法字符的路径抛 ArgumentException。
        // 不接住的话，一个打错的 `--install-dir` 换来的是一段未处理异常栈 —— 而且
        // winexe 双击时那段栈闪都不闪一下就没了。
        try { options = Parse(args, out error); }
        catch (Exception ex) { return Fail("这个路径用不了：" + ex.Message + "\n\n" + Usage, 2, true); }
        if (options == null)
            return Fail(error, 2, true);

        // 守卫和它守着的那一句之间**不留东西**：中间垫几行，判据就只能放宽窗口去找它，
        // 而放宽过的窗口挡不住「守卫被删掉」那一种改法。GUI 的默认值收进 ForAPerson()。
        if (!options.Silent) return InstallWizard.Run(options.ForAPerson());

        try
        {
            InstallOutcome outcome = Install(options, null);
            if (options.Launch)
            {
                var psi = new ProcessStartInfo(outcome.ExePath)
                { UseShellExecute = true, WorkingDirectory = outcome.InstallDir };
                Process.Start(psi);
            }
            return 0;
        }
        catch (InstallRefused refused) { return Fail(refused.Message, refused.Code, false); }
        catch (Exception ex) { return Fail("安装失败：" + ex, 1, false); }
    }
}
