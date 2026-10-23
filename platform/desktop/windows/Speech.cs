// 桌面程序（安装器、卸载器、壳）说给人听的话 —— 两种语言并排写在调用点。
//
// 三种说法：
//   Speech.T("中文", "English")     按当前语言说一种（安装器默认英文，卸载器沿用装的时候选的）
//   Speech.In(lang, "中文", "English")  按指定的语言说（比如卸载项的文件名跟着装的时候的语言）
//   Speech.Both("中文", "English")  中英两行并列 —— 壳起不来时界面还没出来，不知道这个人要哪种
//
// 两种语言写在一起，而不是一张 key 表：分开放的话，改了一边忘了另一边不报错（同前端
// `useT` 的理由）。`test_the_windows_installer_asks_before_it_installs.py` 扫盘守着：
// 编进了这份源码的编译单元里，每一个带中文的字面量都必须是这三种调用的中文参数（写日志的
// Console 输出除外 —— 那不是界面），英文参数里不许有中文。

using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using Microsoft.Win32;

internal static class Speech
{
    public const string Chinese = "zh";
    public const string English = "en";

    /// <summary>"en" 或 "zh"。安装器默认英文（wangd 2026-10-19）；卸载器沿用装的时候选的（install-record.json）。</summary>
    public static string Language = English;

    /// <summary>这一次是不是**人自己选的**（在向导上点过）。</summary>
    /// <remarks>
    /// 只有人自己选过，才把它交给应用第一次打开时用。默认的英文不算选择：系统是中文、
    /// 没碰过这个钮的人，装完打开应用该是中文（应用自己按系统语言定）。
    /// </remarks>
    public static bool ChosenByThePerson = false;

    public static string T(string zh, string en)
    {
        return In(Language, zh, en);
    }

    /// <summary>按指定的语言说（不是当前那种）—— 比如卸载项的文件名跟着装的时候的语言。</summary>
    public static string In(string language, string zh, string en)
    {
        return language == Chinese ? zh : en;
    }

    /// <summary>中英两行并列 —— 界面还没出来、不知道这个人要哪种语言时（壳起不来弹的对话框）。</summary>
    /// <remarks>
    /// wangd 2026-10-19：「对话框中英两行并列」。中文在上，英文在下；任一边是好几行的一段话，
    /// 两段之间空一行 —— 逐句交错比整段并列难读。
    /// </remarks>
    public static string Both(string zh, string en)
    {
        bool paragraphs = zh.Contains("\n") || en.Contains("\n");
        return zh + (paragraphs ? "\r\n\r\n" : "\r\n") + en;
    }

    /// <summary>一句话的两种语言，交给别人去说（比如先拼上日志位置再 Both）。</summary>
    public static Words Pair(string zh, string en)
    {
        return new Words(zh, en);
    }

    /// <summary>一种语言自己的名字，用它自己的文字写 —— 一个只认中文的人看不懂 "Chinese"。</summary>
    public static string NameOf(string language)
    {
        return language == Chinese ? "中文" : "English";
    }

    public static bool Known(string language)
    {
        return language == Chinese || language == English;
    }

    /// <summary>应用第一次打开时读它（后端 `system_language.language_chosen_at_install`）。</summary>
    public const string ChoiceFileName = "install-language";

    /// <summary>这台电脑的系统语言列表里有中文就中文，否则英文 —— 同应用第一次打开的判据。</summary>
    /// <remarks>
    /// 壳的菜单用它（菜单项放不下两行）。列表 = 显示语言 + 「设置 → 时间和语言」里那张语言
    /// 列表：2026-10-19 在 9800x3d 上实测，显示语言是 en-GB、en-US，语言列表是 zh-Hans-CN ——
    /// 只看显示语言会把一个用英文界面的中文用户判成英文（后端 `system_language._windows` 同理）。
    /// </remarks>
    public static string OfThisSystem()
    {
        foreach (string tag in SystemLanguages())
            if (tag.StartsWith("zh-", StringComparison.OrdinalIgnoreCase)
                || string.Equals(tag, "zh", StringComparison.OrdinalIgnoreCase))
                return Chinese;
        return English;
    }

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
    private static extern bool GetUserPreferredUILanguages(uint flags, out uint count, char[] buffer, ref uint size);

    private static List<string> SystemLanguages()
    {
        var found = new List<string>();
        try
        {
            const uint MUI_LANGUAGE_NAME = 0x8;
            uint count, size = 0;
            if (GetUserPreferredUILanguages(MUI_LANGUAGE_NAME, out count, null, ref size) && size > 0)
            {
                var buffer = new char[size];
                if (GetUserPreferredUILanguages(MUI_LANGUAGE_NAME, out count, buffer, ref size))
                    foreach (string tag in new string(buffer).Split('\0'))
                        if (tag.Length > 0) found.Add(tag);
            }
        }
        catch { }
        try
        {
            using (RegistryKey key = Registry.CurrentUser.OpenSubKey(@"Control Panel\International\User Profile"))
            {
                var listed = key == null ? null : key.GetValue("Languages") as string[];
                if (listed != null)
                    foreach (string tag in listed)
                        if (!string.IsNullOrEmpty(tag) && !found.Contains(tag)) found.Add(tag);
            }
        }
        catch { }
        return found;
    }
}

/// <summary>一句话的两种语言。只经 `Speech.Pair` 造 —— 判据认得那一种写法。</summary>
internal struct Words
{
    public readonly string Zh;
    public readonly string En;

    public Words(string zh, string en)
    {
        Zh = zh;
        En = en;
    }
}
