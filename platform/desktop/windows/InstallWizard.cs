// 安装向导（WinForms）：双击 Setup.exe 看见的那三页。
//
// ## 为什么是 WinForms 手写、没有 designer
//
// 整条 Windows 工具链只有 in-box `csc.exe`（目标机无管理员、装不了 VS，RFC #848 §11）。
// 没有 designer、没有 .resx 编译、没有 XAML —— 界面就是一段把控件摆好的代码。所以这里
// 刻意只用最朴素的一套：三个 Panel 叠在一个 Form 上，谁该出现谁 `Visible = true`。
//
// ## 三页分别回答一个问题
//
//   ① 装到哪儿、要不要桌面和开始菜单的图标  →  用户能改的全在这一页
//   ② 现在到哪一步了                        →  解压 800MB 要几十秒，不能是一动不动的窗口
//   ③ 装好了，在哪儿，现在要不要打开它      →  顺带把没做成的事（比如图标没建成）说出来
//
// ## 安装跑在后台线程
//
// 解压是几十秒的阻塞活。跑在 UI 线程上窗口会变成白板、Windows 给它挂上「未响应」——
// 用户以为装挂了就去点关闭。所以 `Install` 在后台线程跑，进度经 `Invoke` 回到 UI 线程；
// 安装期间窗口的关闭按钮是拦住的（`FormClosing`）：这中间没有「取消」这回事 ——
// 真中途杀掉，留下的是一个换到一半的目录。装完之前唯一诚实的答案是「等它装完」。

using System;
using System.Collections.Generic;
using System.Drawing;
using System.IO;
using System.Threading;
using System.Windows.Forms;

internal static class InstallWizard
{
    /// <summary>起向导，返回进程退出码。</summary>
    public static int Run(InstallOptions options)
    {
        Footprint.PrepareUi();
        using (var form = new WizardForm(options))
        {
            Application.Run(form);
            return form.ExitCode;
        }
    }
}

internal sealed class WizardForm : Form
{
    private readonly InstallOptions _options;
    public int ExitCode = 1;   // 用户直接关掉窗口＝没装成

    private readonly Panel _pageChoose = new Panel();
    private readonly Panel _pageProgress = new Panel();
    private readonly Panel _pageDone = new Panel();

    private readonly TextBox _dir = new TextBox();
    private readonly CheckBox _desktop = new CheckBox();
    private readonly CheckBox _startMenu = new CheckBox();
    private readonly Label _space = new Label();
    private readonly Label _complaint = new Label();
    private readonly Button _install = new Button();
    /// <summary>写不进选定位置时才出现：一键换回不需要权限的那个。</summary>
    private readonly LinkLabel _useDefault = new LinkLabel();
    /// <summary>人停下来之后再问一次「装得进去吗」—— 不是每敲一个字符问一次。</summary>
    /// <remarks>
    /// 写全名。这个文件同时 `using System.Threading` 和 `using System.Windows.Forms`，
    /// 两边各有一个 `Timer`，裸写就是 CS0104（而且要到真 csc 上才看得见 —— Mac 这边
    /// 编的是 Swift，2026-09-18 那次 CS0246 就是这么躲过去的）。
    /// 要的是 WinForms 这个：它在 UI 线程上 tick，回调里能直接碰控件。
    /// </remarks>
    private readonly System.Windows.Forms.Timer _recheck = new System.Windows.Forms.Timer();
    private readonly Button _cancel = new Button();

    private readonly Label _step = new Label();
    private readonly ProgressBar _bar = new ProgressBar();

    private readonly Label _doneWhere = new Label();
    private readonly TextBox _doneNotes = new TextBox();
    private readonly CheckBox _launch = new CheckBox();
    private readonly Button _finish = new Button();

    private bool _installing;
    private InstallOutcome _outcome;

    public WizardForm(InstallOptions options)
    {
        _options = options;

        // 下面所有坐标都是 100% 缩放下的像素；EndLayoutAt96Dpi 按本机 DPI 一起放大。
        Footprint.BeginLayoutAt96Dpi(this);
        Speak(() => Text = Speech.T("安装 ScienceMate", "Install ScienceMate"));
        FormBorderStyle = FormBorderStyle.FixedDialog;
        MaximizeBox = false;
        MinimizeBox = true;
        StartPosition = FormStartPosition.CenterScreen;
        ClientSize = new Size(560, 340);
        BackColor = Color.White;
        try { Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath); } catch { }

        BuildChoosePage();
        BuildProgressPage();
        BuildDonePage();
        Controls.Add(_pageChoose);
        Controls.Add(_pageProgress);
        Controls.Add(_pageDone);
        Show(_pageChoose);
        Footprint.EndLayoutAt96Dpi(this, Footprint.UiFont(9f, FontStyle.Regular));
    }

    /// <summary>界面上每一句话都登记在这里 —— 换语言时整页重说一遍。</summary>
    /// <remarks>
    /// 控件只建一次（坐标按 96 DPI 摆好、放大过一次，见 BeginLayoutAt96Dpi），换语言只换字。
    /// 每句话写成一个"再说一遍"的动作：`Speak(() => x.Text = Speech.T("…", "…"))`。
    /// </remarks>
    private readonly List<Action> _speech = new List<Action>();

    private void Speak(Action say)
    {
        _speech.Add(say);
        say();
    }

    /// <summary>第一页顶上的「中文 | English」—— 名字用各自的文字写，认得它的人一眼就认得。</summary>
    private readonly Button _chinese = new Button();
    private readonly Button _english = new Button();

    private void SwitchTo(string language)
    {
        if (Speech.Language == language) return;
        Speech.Language = language;
        Speech.ChosenByThePerson = true;
        SuspendLayout();
        foreach (Action say in _speech) say();
        ShowWhichLanguage();
        // 这两句是现算的（空间、装不装得进），按新语言再算一遍。
        ReportSpace();
        RecheckTheFolder();
        ResumeLayout(true);
    }

    private void ShowWhichLanguage()
    {
        foreach (Button b in new[] { _chinese, _english })
        {
            bool current = (b == _chinese) == (Speech.Language == Speech.Chinese);
            b.FlatAppearance.BorderSize = current ? 1 : 0;
            b.ForeColor = current ? Color.FromArgb(32, 32, 32) : Color.FromArgb(96, 96, 96);
        }
    }

    private Button LanguageButton(Button b, string language, string name, int x)
    {
        b.Text = name;
        b.Location = new Point(x, 18);
        b.Size = new Size(64, 28);   // 同「浏览…」：雅黑 9pt 带边框的按钮在 100% 下要 27 高
        b.FlatStyle = FlatStyle.Flat;
        b.FlatAppearance.BorderColor = Color.FromArgb(160, 160, 160);
        b.BackColor = Color.White;
        b.Cursor = Cursors.Hand;
        b.Click += delegate { SwitchTo(language); };
        return b;
    }

    private void Show(Panel page)
    {
        _pageChoose.Visible = page == _pageChoose;
        _pageProgress.Visible = page == _pageProgress;
        _pageDone.Visible = page == _pageDone;
        page.BringToFront();
    }

    private static Label Heading()
    {
        return new Label
        {
            AutoSize = true,
            Location = new Point(24, 20),
            Font = Footprint.UiFont(14f, FontStyle.Regular),
        };
    }

    private void BuildChoosePage()
    {
        _pageChoose.Dock = DockStyle.Fill;
        var heading = Heading();
        Speak(() => heading.Text = Speech.T("安装 ScienceMate", "Install ScienceMate"));
        _pageChoose.Controls.Add(heading);

        // 语言：默认英文（wangd 2026-10-19），第一页顶上就能换。
        _pageChoose.Controls.Add(LanguageButton(_chinese, Speech.Chinese, Speech.NameOf(Speech.Chinese), 404));
        _pageChoose.Controls.Add(LanguageButton(_english, Speech.English, Speech.NameOf(Speech.English), 472));
        ShowWhichLanguage();

        string version = InstallerVersion.Value;
        var subtitle = new Label
        {
            AutoSize = true,
            ForeColor = Color.FromArgb(96, 96, 96),
            Location = new Point(26, 54),
        };
        Speak(() => subtitle.Text = string.IsNullOrWhiteSpace(version)
            ? Speech.T("选择安装位置。", "Choose where to install.")
            : string.Format(Speech.T("版本 {0}　·　选择安装位置。", "Version {0}  ·  Choose where to install."), version));
        _pageChoose.Controls.Add(subtitle);

        var installTo = new Label { AutoSize = true, Location = new Point(24, 96) };
        Speak(() => installTo.Text = Speech.T("安装到：", "Install to:"));
        _pageChoose.Controls.Add(installTo);

        _dir.Text = _options.InstallDir;
        _dir.Location = new Point(24, 118);
        _dir.Size = new Size(420, 24);
        _pageChoose.Controls.Add(_dir);

        var browse = new Button
        {
            Location = new Point(452, 116),
            Size = new Size(84, 28),   // 雅黑 9pt 的按钮在 100% 下要 27 高，25 会切掉字脚
            UseVisualStyleBackColor = true,
        };
        Speak(() => browse.Text = Speech.T("浏览…", "Browse…"));
        browse.Click += OnBrowse;
        _pageChoose.Controls.Add(browse);

        _space.Location = new Point(24, 148);
        _space.Size = new Size(510, 20);
        _space.ForeColor = Color.FromArgb(96, 96, 96);
        _pageChoose.Controls.Add(_space);

        Speak(() => _desktop.Text = Speech.T("在桌面创建快捷方式", "Create a desktop shortcut"));
        _desktop.Checked = true;
        _desktop.AutoSize = true;
        _desktop.Location = new Point(24, 184);
        _pageChoose.Controls.Add(_desktop);

        Speak(() => _startMenu.Text = Speech.T("添加到开始菜单（含卸载入口）", "Add to the Start menu (with an uninstall entry)"));
        _startMenu.Checked = true;
        _startMenu.AutoSize = true;
        _startMenu.Location = new Point(24, 212);
        _pageChoose.Controls.Add(_startMenu);

        // 三行高（雅黑 9pt 在 100% 下一行 17）：最长的那句（要管理员权限、换成哪个位置、
        // 或者以管理员身份重开）里带着默认安装路径，用户名一长就是三行。只给两行的话，
        // 第三行压在下面那个链接上。下沿正好到按钮那一行（y=292）。
        _complaint.Location = new Point(24, 240);
        _complaint.Size = new Size(512, 52);
        _complaint.ForeColor = Color.FromArgb(176, 32, 32);
        _pageChoose.Controls.Add(_complaint);

        Speak(() => _useDefault.Text = Speech.T("换成不需要管理员权限的位置", "Use a folder that needs no admin rights"));
        _useDefault.Location = new Point(24, 298);   // 与按钮同一行，落在「取消」左边
        _useDefault.Size = new Size(300, 20);
        _useDefault.Visible = false;
        _useDefault.LinkClicked += delegate { _dir.Text = InstallOptions.DefaultInstallDir(); };
        _pageChoose.Controls.Add(_useDefault);

        _recheck.Interval = 400;
        _recheck.Tick += delegate { _recheck.Stop(); RecheckTheFolder(); };

        Speak(() => _install.Text = Speech.T("安装", "Install"));
        _install.Location = new Point(436, 292);
        _install.Size = new Size(100, 30);
        _install.UseVisualStyleBackColor = true;
        _install.Click += OnInstall;
        _pageChoose.Controls.Add(_install);
        AcceptButton = _install;

        Speak(() => _cancel.Text = Speech.T("取消", "Cancel"));
        _cancel.Location = new Point(328, 292);
        _cancel.Size = new Size(100, 30);
        _cancel.UseVisualStyleBackColor = true;
        _cancel.Click += delegate { ExitCode = 7; Close(); };
        _pageChoose.Controls.Add(_cancel);

        _dir.TextChanged += delegate { ReportSpace(); AskAgainShortly(); };
        ReportSpace();
        AskAgainShortly();
    }

    /// <summary>人停下来之后再问一次「这儿装得进去吗」。</summary>
    /// <remarks>
    /// 每敲一个字符就去建一次探针目录，代价落在网络路径和慢盘上（而那正是最可能
    /// 写不进去的两种地方）。停 400ms 再问：人还在打的时候，路径本来也还不成立。
    /// </remarks>
    private void AskAgainShortly()
    {
        _recheck.Stop();
        _recheck.Start();
    }

    /// <summary>这个位置装得进去吗 —— 在他**选完的那一刻**答，不是等他点完安装。</summary>
    /// <remarks>
    /// 2026-09-23 真机（wangd）：路径改成 `C:\Program Files\SM\ScienceMate`，向导
    /// 一句话不说，点了安装、等过一轮，才弹出一句 .NET 原文「对路径"\\?\C:\Program
    /// Files\SM\ScienceMate.new-25e68b40…"的访问被拒绝」—— 里面没有一个字告诉他
    /// **那个位置要管理员权限**，也没告诉他下一步该干什么。
    ///
    /// 同一个形状此前在「建立组织」那张表单上被指出过一次（"密码不合格你当场说啊"）：
    /// 答案本来就在手边，却被推到一次长操作的末尾。这里的答案同样在手边 ——
    /// `WhyNotInstallable` 就是安装第一步要走的那一问，提前问它不花什么。
    ///
    /// 答不上来就**不拦人**（`_install` 留着可点）：探针自己出岔子的时候，
    /// 把一次本来能成的安装挡掉，比让它去撞真正那一步更坏。
    /// </remarks>
    private void RecheckTheFolder()
    {
        string why = null;
        try { why = Installer.WhyNotInstallable(_dir.Text.Trim()); }
        catch { why = null; }
        _complaint.Text = why ?? "";
        _install.Enabled = why == null;
        // 写不进去的时候给一条出路：默认位置不用任何权限，点一下就换过去。
        _useDefault.Visible = why != null && _dir.Text.Trim() != InstallOptions.DefaultInstallDir();
    }

    /// <summary>这个盘还剩多少、大概要多少。</summary>
    /// <remarks>
    /// 只是**说一声**，不拦人：需要多少是按压缩包大小估的（解压比例随内容变），估错了
    /// 去挡住一次本来装得下的安装，比不估更坏。真装不下的时候磁盘会在解压途中报错，
    /// 那是确定的、不会误判的那一个答案。
    /// </remarks>
    private void ReportSpace()
    {
        _complaint.Text = "";
        try
        {
            string root = Path.GetPathRoot(Path.GetFullPath(_dir.Text.Trim()));
            var drive = new DriveInfo(root);
            double freeGb = drive.AvailableFreeSpace / 1024.0 / 1024 / 1024;
            double needGb = InstallerVersion.InstalledBytes / 1024.0 / 1024 / 1024;
            _space.Text = string.Format(Speech.T("需要约 {0:0.0} GB，{1} 可用 {2:0.0} GB",
                                                 "About {0:0.0} GB needed; {2:0.0} GB free on {1}"),
                                        needGb, drive.Name, freeGb);
            _space.ForeColor = freeGb < needGb
                ? Color.FromArgb(176, 32, 32) : Color.FromArgb(96, 96, 96);
        }
        catch { _space.Text = ""; }
    }

    private void OnBrowse(object sender, EventArgs e)
    {
        using (var picker = new FolderBrowserDialog())
        {
            picker.Description = Speech.T("选择一个文件夹，ScienceMate 会装在它下面",
                                          "Choose a folder; ScienceMate will be installed inside it");
            picker.ShowNewFolderButton = true;
            try
            {
                string parent = Path.GetDirectoryName(Path.GetFullPath(_dir.Text.Trim()));
                if (Directory.Exists(parent)) picker.SelectedPath = parent;
            }
            catch { }
            if (picker.ShowDialog(this) != DialogResult.OK) return;
            string chosen = picker.SelectedPath.TrimEnd(Path.DirectorySeparatorChar);
            // 用户选的是「装在哪个文件夹下面」。已经指着一个 ScienceMate 目录就照用，
            // 否则在下面开一个同名目录 —— 别把整个包摊进用户的文档目录里。
            if (!string.Equals(Path.GetFileName(chosen), Footprint.ProductName,
                               StringComparison.OrdinalIgnoreCase))
                chosen = Path.Combine(chosen, Footprint.ProductName);
            _dir.Text = chosen;
        }
    }

    private void OnInstall(object sender, EventArgs e)
    {
        string dir = _dir.Text.Trim();
        string why = Installer.WhyNotInstallable(dir);
        if (why != null) { _complaint.Text = why; return; }

        _options.InstallDir = Path.GetFullPath(dir).TrimEnd(Path.DirectorySeparatorChar);
        _options.DesktopShortcut = _desktop.Checked;
        _options.StartMenuShortcut = _startMenu.Checked;
        _options.RegisterUninstall = true;   // 装了就该在「应用和功能」里找得到

        _installing = true;
        _step.Text = Speech.T("正在准备…", "Getting ready…");
        _bar.Style = ProgressBarStyle.Marquee;
        Show(_pageProgress);

        var worker = new Thread(delegate ()
        {
            try
            {
                InstallOutcome outcome = Installer.Install(_options, Report);
                BeginInvoke((MethodInvoker)delegate { Succeeded(outcome); });
            }
            catch (InstallRefused refused)
            {
                BeginInvoke((MethodInvoker)delegate { Failed(refused.Message, refused.Code); });
            }
            catch (Exception ex)
            {
                BeginInvoke((MethodInvoker)delegate { Failed(Speech.T("安装失败：", "Installation failed: ") + ex.Message, 1); });
            }
        });
        worker.IsBackground = true;
        worker.SetApartmentState(ApartmentState.STA);   // 建快捷方式要走 COM
        worker.Start();
    }

    /// <summary>后台线程报进度；负的 fraction ＝ 说不出百分比，用滚动条。</summary>
    private void Report(string what, double fraction)
    {
        try
        {
            BeginInvoke((MethodInvoker)delegate
            {
                _step.Text = what;
                if (fraction < 0) { _bar.Style = ProgressBarStyle.Marquee; return; }
                _bar.Style = ProgressBarStyle.Continuous;
                _bar.Value = (int)Math.Max(0, Math.Min(100, fraction * 100));
            });
        }
        // 窗口已经没了就别报了。ObjectDisposedException 是 InvalidOperationException
        // 的子类，这一条把两种都接住。
        catch (InvalidOperationException) { }
    }

    private void BuildProgressPage()
    {
        _pageProgress.Dock = DockStyle.Fill;
        var heading = Heading();
        Speak(() => heading.Text = Speech.T("正在安装", "Installing"));
        _pageProgress.Controls.Add(heading);

        _step.Location = new Point(26, 140);
        _step.Size = new Size(510, 22);
        _pageProgress.Controls.Add(_step);

        _bar.Location = new Point(26, 166);
        _bar.Size = new Size(510, 22);
        _bar.Style = ProgressBarStyle.Marquee;
        _pageProgress.Controls.Add(_bar);

        var patience = new Label
        {
            AutoSize = true,
            ForeColor = Color.FromArgb(96, 96, 96),
            Location = new Point(26, 196),
        };
        Speak(() => patience.Text = Speech.T("首次安装要解压约 1 GB 文件，请稍候。",
                                             "A first install unpacks about 1 GB of files — please wait."));
        _pageProgress.Controls.Add(patience);
    }

    private void BuildDonePage()
    {
        _pageDone.Dock = DockStyle.Fill;
        var heading = Heading();
        Speak(() => heading.Text = Speech.T("安装完成", "Installed"));
        _pageDone.Controls.Add(heading);

        _doneWhere.Location = new Point(26, 64);
        _doneWhere.Size = new Size(510, 40);
        _pageDone.Controls.Add(_doneWhere);

        // 只读多行框而不是 Label：没做成的事可能有好几条，也可能一条没有。
        _doneNotes.Location = new Point(26, 110);
        _doneNotes.Size = new Size(510, 108);
        _doneNotes.Multiline = true;
        _doneNotes.ReadOnly = true;
        _doneNotes.ScrollBars = ScrollBars.Vertical;
        _doneNotes.BackColor = Color.FromArgb(250, 245, 235);
        _doneNotes.BorderStyle = BorderStyle.FixedSingle;
        _doneNotes.Visible = false;
        _pageDone.Controls.Add(_doneNotes);

        Speak(() => _launch.Text = Speech.T("立即运行 ScienceMate", "Run ScienceMate now"));
        _launch.Checked = true;
        _launch.AutoSize = true;
        _launch.Location = new Point(26, 250);
        _pageDone.Controls.Add(_launch);

        Speak(() => _finish.Text = Speech.T("完成", "Finish"));
        _finish.Location = new Point(436, 292);
        _finish.Size = new Size(100, 30);
        _finish.UseVisualStyleBackColor = true;
        _finish.Click += OnFinish;
        _pageDone.Controls.Add(_finish);
    }

    private void Succeeded(InstallOutcome outcome)
    {
        _installing = false;
        _outcome = outcome;
        _doneWhere.Text = Speech.T("已安装到：", "Installed to: ") + outcome.InstallDir;
        if (outcome.Warnings.Count > 0)
        {
            _doneNotes.Text = Speech.T("有几件事没做成（不影响使用）：", "A few things did not work out (ScienceMate still works):")
                              + "\r\n\r\n· "
                              + string.Join("\r\n· ", outcome.Warnings.ToArray());
            _doneNotes.Visible = true;
        }
        AcceptButton = _finish;
        Show(_pageDone);
        _finish.Focus();
    }

    private void Failed(string message, int code)
    {
        _installing = false;
        ExitCode = code;
        // **先回第一页，再弹框**。反过来的话，模态框把 UI 线程停在这一句上，用户身后是
        // 一个写着「正在安装／正在就位…」的窗口 —— 装失败了，它还在说正在装。
        // 错的多半是「位置选得不合适」或「应用正开着」，两样都是改一下就能再来的。
        _complaint.Text = message.Split('\n')[0];
        AcceptButton = _install;
        Show(_pageChoose);
        Footprint.MessageBoxW(Handle, message, Speech.T("ScienceMate 安装", "ScienceMate Setup"), Footprint.IconError);
    }

    private void OnFinish(object sender, EventArgs e)
    {
        ExitCode = 0;
        if (_launch.Checked && _outcome != null)
        {
            try
            {
                System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo(_outcome.ExePath)
                { UseShellExecute = true, WorkingDirectory = _outcome.InstallDir });
            }
            catch (Exception ex)
            {
                Footprint.MessageBoxW(Handle, Speech.T("装好了，但没能启动：", "Installed, but it could not be started: ")
                                      + ex.Message + "\n\n"
                                      + Speech.T("可以到桌面或开始菜单里点它。",
                                                 "You can open it from the desktop or the Start menu."),
                                      "ScienceMate", Footprint.IconWarning);
            }
        }
        Close();
    }

    protected override void OnFormClosing(FormClosingEventArgs e)
    {
        // 安装到一半关窗＝留下一个换到一半的目录。这中间没有「取消」这回事。
        if (_installing && e.CloseReason == CloseReason.UserClosing) { e.Cancel = true; return; }
        base.OnFormClosing(e);
    }
}
