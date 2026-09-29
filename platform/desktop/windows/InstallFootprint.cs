// 安装留下的痕迹：快捷方式、「应用和功能」里的那一条、以及记下这两样的清单。
//
// ## 为什么装完要留一份清单
//
// 卸载器要删的东西里，只有安装目录是它自己站着的地方；桌面/开始菜单的快捷方式、
// 注册表那一条，都在**别人的地盘**上。按「默认位置」去猜着删，猜错一次就是删掉
// 不属于这次安装的东西 —— 用户装了两份（一份 `%LOCALAPPDATA%`、一份 `D:\`），
// 卸载其中一份会把另一份的桌面图标一起带走。
//
// 所以安装的时候把「我究竟在哪儿放了什么」写成 `install-record.json` 放进安装目录，
// 卸载时照单删。清单丢了（手工拷贝的安装、更早版本装的）也不至于寸步难行：卸载器
// 会退回默认位置去找，但**删之前一律核一眼这个快捷方式指向的是不是我自己**
// （`ShortcutTarget`），核不上就留着。宁可留下一个孤儿图标，也不删别人的东西。
//
// ## 快捷方式为什么走 IShellLinkW，不走 WScript.Shell
//
// 壳和安装器都只能用 in-box `csc.exe` 编（目标机无管理员、无 VS，见 RFC #848 §11），
// 手上没有任何 COM interop 程序集，也没有 tlbimp。两条路都不需要它们：WSH 那条靠
// 反射晚绑定（`Type.GetTypeFromProgID` + `InvokeMember`），shell32 这条靠手写
// `[ComImport]` 接口声明。第一版写的是 WSH —— 少几十行。
//
// 2026-09-21 真机把它否掉了。WScript.Shell 的 `CreateShortcut` 走 **ANSI**：
//
//   · 文件名含中文 → `Unable to save shortcut "…\?? ScienceMate.lnk"`
//     （「卸载 ScienceMate.lnk」在它手里先变成了 `?? ScienceMate.lnk`）
//   · **目标路径**含中文 → `Value does not fall within the expected range.`，
//     PowerShell 里跑同一个 COM 对象一模一样地失败。
//
// 第二条才是要命的：默认安装位置是 `%LOCALAPPDATA%\Programs\ScienceMate`，中文
// Windows 上用户名常常就是中文（`C:\Users\张三\AppData\Local\…`）—— 那台机器上
// **一个快捷方式都建不出来**，而这正是用户要的东西。这不是边角情况，是中文用户的常态。
//
// `IShellLinkW` 是 shell 自己的 Unicode 接口（`SetPath` / `Save` 全是 LPWStr），
// 上面两条在它手里都通过：中文目标 + 中文文件名，写进去读回来一致。接口声明手写在
// 下面 —— 方法顺序就是 vtable 顺序，**不能重排、不能省略**（省一个就把后面全错位了）。
//
// 建快捷方式仍然可能失败（磁盘、权限、策略）。失败了就是**建不出快捷方式**，不是
// **装不上** —— 这两件事的严重程度差着量级，所以失败一路带回调用方当作可报告的结果，
// 安装本身照常完成（向导在完成页说一句「桌面快捷方式没建成」，静默安装写 stderr）。

using System;
using System.Collections.Generic;
using System.Drawing;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Web.Script.Serialization;
using System.Windows.Forms;

using Microsoft.Win32;

/// <summary>shell 的快捷方式对象（CLSID_ShellLink）。</summary>
[ComImport, Guid("00021401-0000-0000-C000-000000000046")]
internal class ShellLinkObject { }

/// <summary>`IShellLinkW` —— 方法顺序即 vtable 顺序，别重排、别省略。</summary>
[ComImport, Guid("000214F9-0000-0000-C000-000000000046"),
 InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
internal interface IShellLinkW
{
    void GetPath([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder pszFile, int cch, IntPtr pfd, uint fFlags);
    void GetIDList(out IntPtr ppidl);
    void SetIDList(IntPtr pidl);
    void GetDescription([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder pszName, int cch);
    void SetDescription([MarshalAs(UnmanagedType.LPWStr)] string pszName);
    void GetWorkingDirectory([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder pszDir, int cch);
    void SetWorkingDirectory([MarshalAs(UnmanagedType.LPWStr)] string pszDir);
    void GetArguments([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder pszArgs, int cch);
    void SetArguments([MarshalAs(UnmanagedType.LPWStr)] string pszArgs);
    void GetHotkey(out short pwHotkey);
    void SetHotkey(short wHotkey);
    void GetShowCmd(out int piShowCmd);
    void SetShowCmd(int iShowCmd);
    void GetIconLocation([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder pszIconPath, int cch, out int piIcon);
    void SetIconLocation([MarshalAs(UnmanagedType.LPWStr)] string pszIconPath, int iIcon);
    void SetRelativePath([MarshalAs(UnmanagedType.LPWStr)] string pszPathRel, uint dwReserved);
    void Resolve(IntPtr hwnd, uint fFlags);
    void SetPath([MarshalAs(UnmanagedType.LPWStr)] string pszFile);
}

/// <summary>`IPersistFile`（前三个方法是 IPersist 的）—— .lnk 的落盘与读回。</summary>
[ComImport, Guid("0000010b-0000-0000-C000-000000000046"),
 InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
internal interface IPersistFile
{
    void GetClassID(out Guid pClassID);
    [PreserveSig] int IsDirty();
    void Load([MarshalAs(UnmanagedType.LPWStr)] string pszFileName, uint dwMode);
    void Save([MarshalAs(UnmanagedType.LPWStr)] string pszFileName, [MarshalAs(UnmanagedType.Bool)] bool fRemember);
    void SaveCompleted([MarshalAs(UnmanagedType.LPWStr)] string pszFileName);
    void GetCurFile([MarshalAs(UnmanagedType.LPWStr)] out string ppszFileName);
}

/// <summary>装出来的一份 ScienceMate 在这台机器上留下的全部痕迹。</summary>
internal sealed class InstallRecord
{
    public string installDir;
    public string version;
    /// <summary>建成了的快捷方式的绝对路径（建失败的不在里面 —— 清单记的是事实，不是打算）。</summary>
    public List<string> shortcuts = new List<string>();
    /// <summary>HKCU 下「应用和功能」那一条的子键路径；没注册则为 null。</summary>
    public string uninstallKey;
    /// <summary>研究数据放哪（卸载时问用户留不留）。</summary>
    public string dataRoot;

    public const string FileName = "install-record.json";

    public static string PathIn(string installDir)
    {
        return Path.Combine(installDir, FileName);
    }

    public void Save(string dir)
    {
        string json = new JavaScriptSerializer().Serialize(new Dictionary<string, object>
        {
            { "installDir", installDir },
            { "version", version },
            { "shortcuts", shortcuts },
            { "uninstallKey", uninstallKey },
            { "dataRoot", dataRoot },
        });
        File.WriteAllText(Footprint.IoPath(PathIn(dir)), json, new UTF8Encoding(false));
    }

    /// <summary>读安装目录里的清单；没有或读不懂就返回 null（调用方退回默认位置）。</summary>
    public static InstallRecord Load(string dir)
    {
        try
        {
            string path = Footprint.IoPath(PathIn(dir));
            if (!File.Exists(path)) return null;
            var parsed = new JavaScriptSerializer()
                .Deserialize<Dictionary<string, object>>(File.ReadAllText(path, Encoding.UTF8));
            if (parsed == null) return null;
            var record = new InstallRecord
            {
                installDir = Text(parsed, "installDir"),
                version = Text(parsed, "version"),
                uninstallKey = Text(parsed, "uninstallKey"),
                dataRoot = Text(parsed, "dataRoot"),
            };
            object list;
            if (parsed.TryGetValue("shortcuts", out list) && list is System.Collections.IEnumerable)
                foreach (object item in (System.Collections.IEnumerable)list)
                {
                    string s = Convert.ToString(item);
                    if (!string.IsNullOrWhiteSpace(s)) record.shortcuts.Add(s);
                }
            return record;
        }
        catch { return null; }
    }

    private static string Text(Dictionary<string, object> parsed, string key)
    {
        object value;
        if (!parsed.TryGetValue(key, out value) || value == null) return null;
        string text = Convert.ToString(value);
        return string.IsNullOrWhiteSpace(text) ? null : text;
    }
}

/// <summary>快捷方式与「应用和功能」——安装器写，卸载器读和删。两边同一份源码，不会分叉。</summary>
internal static class Footprint
{
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    public static extern int MessageBoxW(IntPtr hWnd, string text, string caption, uint type);

    public const uint IconError = 0x10;
    public const uint IconWarning = 0x30;
    public const uint IconInfo = 0x40;

    [DllImport("user32.dll")]
    private static extern bool SetProcessDPIAware();

    /// <summary>开窗之前要做的三件事：DPI、视觉样式、文本渲染。</summary>
    /// <remarks>
    /// 安装器和卸载器共用 —— 漏在一边的话，两个窗口在同一台机器上长得不一样：
    /// 没 `EnableVisualStyles` 的那个是 Windows 经典外观的按钮，没 `SetProcessDPIAware`
    /// 的那个在高 DPI 屏上是被系统拉伸出来的糊图。用户看到的是同一个产品的两个界面。
    /// </remarks>
    public static void PrepareUi()
    {
        try { if (Environment.OSVersion.Version.Major >= 6) SetProcessDPIAware(); } catch { }
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
    }

    /// <summary>界面字体：有雅黑用雅黑（中文界面），没有就用系统消息框的字体。</summary>
    /// <remarks>安装器和卸载器共用 —— 两个窗口不该长得不一样。</remarks>
    public static Font UiFont(float size, FontStyle style)
    {
        try
        {
            var font = new Font("Microsoft YaHei UI", size, style);
            if (font.Name == "Microsoft YaHei UI") return font;
            font.Dispose();
        }
        catch { }
        return new Font(SystemFonts.MessageBoxFont.FontFamily, size, style);
    }

    /// <summary>「应用和功能」列表在 HKCU 下的位置。per-user 安装注册在 HKCU，不需要管理员。</summary>
    public const string DefaultUninstallKey =
        @"Software\Microsoft\Windows\CurrentVersion\Uninstall\ScienceMate";

    public const string ProductName = "ScienceMate";
    public const string ShellExeName = "ScienceMate.exe";
    public const string UninstallerExeName = "Uninstall.exe";

    // 和 InstallerStub/Shell 同一条规矩：System.IO 走扩展路径，不受 MAX_PATH 策略摆布。
    public static string IoPath(string path)
    {
        if (path.StartsWith(@"\\?\", StringComparison.Ordinal)) return path;
        string absolute = Path.GetFullPath(path);
        return absolute.StartsWith(@"\\", StringComparison.Ordinal)
            ? @"\\?\UNC\" + absolute.Substring(2) : @"\\?\" + absolute;
    }

    /// <summary>数据根：和 Shell.cs `DataRoot()`、app/config.py 同一条规则。</summary>
    public static string DataRoot()
    {
        string configured = Environment.GetEnvironmentVariable("HARNESS_FRAMEWORK_HOME");
        return string.IsNullOrWhiteSpace(configured)
            ? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "afs")
            : Path.GetFullPath(configured);
    }

    /// <summary>桌面目录。打包自检要把图标落到隔离目录里去，所以留一个覆盖口。</summary>
    public static string DesktopDir(string overrideDir)
    {
        return string.IsNullOrWhiteSpace(overrideDir)
            ? Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory)
            : Path.GetFullPath(overrideDir);
    }

    /// <summary>开始菜单里属于本产品的那个文件夹（`…\Programs\ScienceMate`）。</summary>
    public static string StartMenuDir(string overrideDir)
    {
        string programs = string.IsNullOrWhiteSpace(overrideDir)
            ? Environment.GetFolderPath(Environment.SpecialFolder.Programs)
            : Path.GetFullPath(overrideDir);
        return Path.Combine(programs, ProductName);
    }

    /// <summary>建一个 .lnk。建不成抛异常 —— 调用方决定这是否该让安装失败（它不该）。</summary>
    public static void CreateShortcut(string lnkPath, string target, string workingDirectory,
                                      string description, string arguments)
    {
        Directory.CreateDirectory(IoPath(Path.GetDirectoryName(lnkPath)));
        var link = (IShellLinkW)new ShellLinkObject();
        try
        {
            link.SetPath(target);
            if (!string.IsNullOrEmpty(workingDirectory)) link.SetWorkingDirectory(workingDirectory);
            if (!string.IsNullOrEmpty(description)) link.SetDescription(description);
            if (!string.IsNullOrEmpty(arguments)) link.SetArguments(arguments);
            link.SetIconLocation(target, 0);
            // `Save` 要普通路径：`\\?\` 前缀 shell 不认。
            ((IPersistFile)link).Save(Path.GetFullPath(lnkPath), true);
        }
        finally { Marshal.FinalReleaseComObject(link); }
    }

    /// <summary>这个 .lnk 指向哪个 exe；读不出来返回 null。</summary>
    /// <remarks>卸载器删快捷方式之前问的就是这一句 —— 「它指的是我吗」。</remarks>
    public static string ShortcutTarget(string lnkPath)
    {
        if (!File.Exists(IoPath(lnkPath))) return null;
        IShellLinkW link = null;
        try
        {
            link = (IShellLinkW)new ShellLinkObject();
            ((IPersistFile)link).Load(Path.GetFullPath(lnkPath), 0);
            var buffer = new StringBuilder(1024);
            // SLGP_RAWPATH：要写进去的那个路径本身，不要 shell 替我们「解析」过的版本 ——
            // 解析会去追已经不在了的目标，把答案变成另一个问题的答案。
            link.GetPath(buffer, buffer.Capacity, IntPtr.Zero, 4);
            string text = buffer.ToString();
            return string.IsNullOrWhiteSpace(text) ? null : text;
        }
        catch { return null; }
        finally { if (link != null) Marshal.FinalReleaseComObject(link); }
    }

    /// <summary>往「应用和功能」里写这一条。</summary>
    public static void RegisterUninstallEntry(string keyPath, string installDir, string version, long sizeKilobytes)
    {
        using (RegistryKey key = Registry.CurrentUser.CreateSubKey(keyPath))
        {
            if (key == null) throw new InvalidOperationException("写不了注册表项 HKCU\\" + keyPath);
            string uninstaller = Path.Combine(installDir, UninstallerExeName);
            key.SetValue("DisplayName", ProductName);
            if (!string.IsNullOrWhiteSpace(version)) key.SetValue("DisplayVersion", version);
            key.SetValue("Publisher", "Agent for Science");
            key.SetValue("InstallLocation", installDir);
            key.SetValue("DisplayIcon", Path.Combine(installDir, ShellExeName));
            key.SetValue("UninstallString", "\"" + uninstaller + "\"");
            key.SetValue("QuietUninstallString", "\"" + uninstaller + "\" /S");
            key.SetValue("EstimatedSize", (int)Math.Min(sizeKilobytes, int.MaxValue), RegistryValueKind.DWord);
            key.SetValue("NoModify", 1, RegistryValueKind.DWord);
            key.SetValue("NoRepair", 1, RegistryValueKind.DWord);
            key.SetValue("InstallDate", DateTime.Now.ToString("yyyyMMdd"), RegistryValueKind.String);
        }
    }

    /// <summary>删「应用和功能」那一条 —— 但只删属于这个安装目录的那一条。</summary>
    /// <remarks>
    /// 同一个用户装了两份，注册表这一条只有一个位置，后装的会盖掉先装的。卸载先装的那份
    /// 时，键里写的 `InstallLocation` 已经指着后装的那份了 —— 这时候删掉它，等于把还活着
    /// 的那份从「应用和功能」里抹掉。所以删之前核一眼它指的是不是自己。
    /// </remarks>
    public static bool RemoveUninstallEntry(string keyPath, string installDir)
    {
        using (RegistryKey key = Registry.CurrentUser.OpenSubKey(keyPath))
        {
            if (key == null) return false;
            string registered = Convert.ToString(key.GetValue("InstallLocation"));
            if (!SamePath(registered, installDir)) return false;
        }
        Registry.CurrentUser.DeleteSubKeyTree(keyPath, false);
        return true;
    }

    public static bool SamePath(string a, string b)
    {
        if (string.IsNullOrWhiteSpace(a) || string.IsNullOrWhiteSpace(b)) return false;
        try
        {
            return string.Equals(
                Path.GetFullPath(a).TrimEnd(Path.DirectorySeparatorChar),
                Path.GetFullPath(b).TrimEnd(Path.DirectorySeparatorChar),
                StringComparison.OrdinalIgnoreCase);
        }
        catch { return false; }
    }

    /// <summary>安装目录占多少 KB（给「应用和功能」显示用）。</summary>
    public static long KilobytesOnDisk(string dir)
    {
        long bytes = 0;
        try
        {
            foreach (string file in Directory.EnumerateFiles(IoPath(dir), "*", SearchOption.AllDirectories))
            {
                try { bytes += new FileInfo(file).Length; } catch { }
            }
        }
        catch { }
        return bytes / 1024;
    }

    /// <summary>安装目录里那份 `backend.json` 写的版本号；读不出来返回 null。</summary>
    public static string VersionIn(string installDir)
    {
        try
        {
            string path = IoPath(Path.Combine(installDir, "backend.json"));
            if (!File.Exists(path)) return null;
            var parsed = new JavaScriptSerializer()
                .Deserialize<Dictionary<string, object>>(File.ReadAllText(path, Encoding.UTF8));
            object version;
            if (parsed == null || !parsed.TryGetValue("version", out version)) return null;
            string text = Convert.ToString(version);
            return string.IsNullOrWhiteSpace(text) ? null : text;
        }
        catch { return null; }
    }
}
