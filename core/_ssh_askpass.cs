using System;
using System.Text;

internal static class SSHAskPass
{
    private static void Main()
    {
        Console.OutputEncoding = new UTF8Encoding(false);
        Console.Write(Environment.GetEnvironmentVariable("HF_SSH_PASSWORD") ?? "");
    }
}
