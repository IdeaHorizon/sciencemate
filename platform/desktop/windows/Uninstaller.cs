// 卸载器：装到哪儿它就在哪儿（`<安装目录>\Uninstall.exe`），开始菜单和「应用和功能」
// 里的卸载都指向它。
//
// ## 它删什么、凭什么删
//
// 删三样：安装目录、这次安装建的快捷方式、「应用和功能」里的那一条。数据（默认
// `%LOCALAPPDATA%\afs`：研究课题、产出、配置）**默认不删** —— 卸载一个程序和扔掉
// 自己的研究结果是两件事，后者得用户明确说要。
//
// 删快捷方式和注册表项之前一律先问「它指的是我吗」（`ShortcutTarget` /
// `InstallLocation`）：同一个用户可以装两份（一份 `%LOCALAPPDATA%`、一份 `D:\`），
// 按默认位置闭眼删会把还活着的那份的桌面图标一起带走。宁可留一个孤儿图标。
//
// ## 为什么要有第二阶段
//
// 要删的目录就是自己所在的目录 —— 一个正在跑的 exe 删不掉自己。所以：把自己拷一份到
// `%TEMP%`，让那个副本带着 `--second-stage <安装目录>` 起来，自己退出；副本等原进程
// 退场后把目录删干净。副本留在 %TEMP% 里不管（那是系统会清的地方），不为了删掉它
// 再去生出第三个进程。

using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Reflection;
using System.Text;
using System.Threading;
using System.Windows.Forms;

internal static class Uninstaller
{
    private const string Caption = "卸载 ScienceMate";

    private static int Say(string message, bool interactive, int code, uint icon)
    {
        if (code != 0) Console.Error.WriteLine(message); else Console.WriteLine(message);
        if (interactive)
        {
            try { Footprint.MessageBoxW(IntPtr.Zero, message, Caption, icon); } catch { }
        }
        return code;
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

    /// <summary>这份安装有没有正在跑的实例。按 exe 全路径认，不按进程名认。</summary>
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
            catch { }
        }
        return 0;
    }

    /// <summary>这次安装建的快捷方式。清单里有就照清单，没有就按默认位置猜 —— 但删之前都要核对指向。</summary>
    private static List<string> ShortcutsOf(InstallRecord record, string installDir)
    {
        var paths = new List<string>();
        if (record != null && record.shortcuts.Count > 0) paths.AddRange(record.shortcuts);
        else
        {
            paths.Add(Path.Combine(Footprint.DesktopDir(null), Footprint.ProductName + ".lnk"));
            string startMenu = Footprint.StartMenuDir(null);
            paths.Add(Path.Combine(startMenu, Footprint.ProductName + ".lnk"));
            paths.Add(Path.Combine(startMenu, "卸载 " + Footprint.ProductName + ".lnk"));
        }
        return paths;
    }

    /// <summary>删掉指向这份安装的快捷方式；返回没删成的说明。</summary>
    private static List<string> RemoveShortcuts(List<string> paths, string installDir)
    {
        var notes = new List<string>();
        var folders = new List<string>();
        foreach (string lnk in paths)
        {
            try
            {
                if (!File.Exists(Footprint.IoPath(lnk))) continue;
                string target = Footprint.ShortcutTarget(lnk);
                bool mine = target != null
                    && (Footprint.SamePath(target, Path.Combine(installDir, Footprint.ShellExeName))
                        || Footprint.SamePath(target, Path.Combine(installDir, Footprint.UninstallerExeName)));
                if (!mine)
                {
                    notes.Add("留下了 " + lnk + "（它指向的不是这份安装）");
                    continue;
                }
                File.Delete(Footprint.IoPath(lnk));
                string folder = Path.GetDirectoryName(lnk);
                if (!folders.Contains(folder)) folders.Add(folder);
            }
            catch (Exception ex) { notes.Add("删不掉 " + lnk + "：" + ex.Message); }
        }
        // 开始菜单里那个产品文件夹：空了才删 —— 里面要是还有别人的东西，那就不是我的文件夹。
        foreach (string folder in folders)
        {
            try
            {
                string io = Footprint.IoPath(folder);
                if (string.Equals(Path.GetFileName(folder.TrimEnd(Path.DirectorySeparatorChar)),
                                  Footprint.ProductName, StringComparison.OrdinalIgnoreCase)
                    && Directory.Exists(io) && Directory.GetFileSystemEntries(io).Length == 0)
                    Directory.Delete(io);
            }
            catch { }
        }
        return notes;
    }

    /// <summary>删一棵目录树，给文件锁留点时间。</summary>
    private static bool DeleteTree(string dir, TimeSpan patience)
    {
        string io = Footprint.IoPath(dir);
        DateTime deadline = DateTime.UtcNow + patience;
        while (true)
        {
            try
            {
                if (!Directory.Exists(io)) return true;
                Directory.Delete(io, true);
                return true;
            }
            catch
            {
                if (DateTime.UtcNow >= deadline) return !Directory.Exists(io);
                Thread.Sleep(400);
            }
        }
    }

    /// <summary>第二阶段：站在 %TEMP% 里，把安装目录（以及用户点了要删的数据）删掉。</summary>
    private static int SecondStage(string installDir, string purgeDataRoot, bool interactive)
    {
        // 等第一阶段那个进程退场 —— 它还在的时候目录删不动。
        Thread.Sleep(500);
        var notes = new List<string>();
        if (!DeleteTree(installDir, TimeSpan.FromSeconds(20)))
            notes.Add("安装目录没能完全删掉：" + installDir);
        if (!string.IsNullOrEmpty(purgeDataRoot) && !DeleteTree(purgeDataRoot, TimeSpan.FromSeconds(20)))
            notes.Add("数据目录没能完全删掉：" + purgeDataRoot);

        if (notes.Count > 0)
            return Say("ScienceMate 已卸载，但有东西没删干净：\n\n· " + string.Join("\n· ", notes.ToArray())
                       + "\n\n关掉可能占用它的程序后手动删除即可。", interactive, 8, 0x30);
        return Say("ScienceMate 已卸载。", interactive, 0, 0x40);
    }

    /// <summary>把自己拷到 %TEMP% 并让副本接手删目录这件事。</summary>
    private static int HandOffToSecondStage(string installDir, string purgeDataRoot, bool interactive)
    {
        string copy = Path.Combine(Path.GetTempPath(),
                                   "sciencemate-uninstall-" + Guid.NewGuid().ToString("N") + ".exe");
        File.Copy(Assembly.GetExecutingAssembly().Location, copy, true);
        var args = new StringBuilder();
        args.Append("--second-stage \"").Append(installDir).Append("\"");
        if (!string.IsNullOrEmpty(purgeDataRoot))
            args.Append(" --purge-data \"").Append(purgeDataRoot).Append("\"");
        if (!interactive) args.Append(" /S");
        Process.Start(new ProcessStartInfo(copy, args.ToString()) { UseShellExecute = false });
        return 0;
    }

    [STAThread]
    private static int Main(string[] args)
    {
        AppContext.SetSwitch("Switch.System.IO.UseLegacyPathHandling", false);
        AppContext.SetSwitch("Switch.System.IO.BlockLongPaths", false);
        SpeakUtf8();

        bool interactive = true;
        bool purgeData = false;
        string secondStage = null, purgeDataRoot = null;
        for (int i = 0; i < args.Length; i++)
        {
            switch (args[i])
            {
                case "/S":
                case "--silent": interactive = false; break;
                case "--purge-data":
                    // 第一阶段当旗标用（静默卸载要不要连数据一起删），第二阶段后面跟着路径。
                    if (i + 1 < args.Length && !args[i + 1].StartsWith("-") && !args[i + 1].StartsWith("/"))
                        purgeDataRoot = args[++i];
                    purgeData = true;
                    break;
                case "--second-stage":
                    if (++i >= args.Length) return Say("用法：Uninstall.exe [/S] [--purge-data]", true, 2, 0x10);
                    secondStage = args[i];
                    break;
                default:
                    return Say("不认识的参数：" + args[i] + "\n\n用法：Uninstall.exe [/S] [--purge-data]",
                               interactive, 2, 0x10);
            }
        }

        if (secondStage != null)
            return SecondStage(secondStage, purgeDataRoot, interactive);

        string installDir = Path.GetDirectoryName(
            Path.GetFullPath(Assembly.GetExecutingAssembly().Location)).TrimEnd(Path.DirectorySeparatorChar);
        InstallRecord record = InstallRecord.Load(installDir);
        string dataRoot = record != null && !string.IsNullOrWhiteSpace(record.dataRoot)
            ? record.dataRoot : Footprint.DataRoot();

        int running = RunningPidIn(installDir);
        if (running != 0)
            return Say("ScienceMate 正在运行（pid " + running + "）。\n\n请先关掉它，再卸载。\n"
                       + "现在什么都没有改动。", interactive, 4, 0x10);

        if (interactive)
        {
            Footprint.PrepareUi();
            using (var ask = new ConfirmUninstall(installDir, dataRoot))
            {
                if (ask.ShowDialog() != DialogResult.OK) return 7;
                purgeData = ask.PurgeData;
            }
        }

        var notes = RemoveShortcuts(ShortcutsOf(record, installDir), installDir);
        string key = record != null && !string.IsNullOrWhiteSpace(record.uninstallKey)
            ? record.uninstallKey : Footprint.DefaultUninstallKey;
        try { Footprint.RemoveUninstallEntry(key, installDir); }
        catch (Exception ex) { notes.Add("「应用和功能」里的那一条没删掉：" + ex.Message); }
        foreach (string note in notes) Console.Error.WriteLine(note);

        // 数据默认留着。用户点了「连数据一起删」才把它交给第二阶段。
        return HandOffToSecondStage(installDir, purgeData ? dataRoot : null, interactive);
    }
}

/// <summary>卸载前问一句 —— 以及把「研究数据要不要一起删」摆在明处。</summary>
internal sealed class ConfirmUninstall : Form
{
    private readonly CheckBox _purge = new CheckBox();
    public bool PurgeData { get { return _purge.Checked; } }

    public ConfirmUninstall(string installDir, string dataRoot)
    {
        Text = "卸载 ScienceMate";
        FormBorderStyle = FormBorderStyle.FixedDialog;
        MaximizeBox = false;
        MinimizeBox = false;
        StartPosition = FormStartPosition.CenterScreen;
        ClientSize = new Size(520, 230);
        BackColor = Color.White;
        Font = Footprint.UiFont(9f, FontStyle.Regular);

        Controls.Add(new Label
        {
            Text = "要卸载 ScienceMate 吗？",
            AutoSize = true,
            Font = Footprint.UiFont(13f, FontStyle.Regular),
            Location = new Point(24, 22),
        });
        Controls.Add(new Label
        {
            Text = "将删除：" + installDir + "\r\n以及桌面和开始菜单里的快捷方式。",
            Location = new Point(26, 62),
            Size = new Size(470, 44),
            ForeColor = Color.FromArgb(96, 96, 96),
        });

        _purge.Text = "同时删除研究数据（" + dataRoot + "）";
        _purge.Location = new Point(26, 118);
        _purge.Size = new Size(470, 36);
        Controls.Add(_purge);
        Controls.Add(new Label
        {
            Text = "不勾选＝课题、产出和配置都留着，重新安装后还在。",
            Location = new Point(44, 150),
            Size = new Size(452, 20),
            ForeColor = Color.FromArgb(96, 96, 96),
        });

        var ok = new Button
        {
            Text = "卸载",
            Location = new Point(396, 184),
            Size = new Size(100, 30),
            DialogResult = DialogResult.OK,
            UseVisualStyleBackColor = true,
        };
        var cancel = new Button
        {
            Text = "取消",
            Location = new Point(288, 184),
            Size = new Size(100, 30),
            DialogResult = DialogResult.Cancel,
            UseVisualStyleBackColor = true,
        };
        Controls.Add(ok);
        Controls.Add(cancel);
        AcceptButton = cancel;   // 默认落在「取消」上：回车不该把人的安装删掉
        CancelButton = cancel;
    }
}
