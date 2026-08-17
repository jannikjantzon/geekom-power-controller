import { Injectable } from "@nestjs/common";
import { spawn } from "node:child_process";

type PowerAction = "wake" | "status" | "shutdown";

export type PowerControllerResult = {
    schemaVersion: 1;
    action: PowerAction;
    ok: boolean;
    exitCode: number;
    failedDeviceIds: string[];
    failedMacs: string[];
    results?: Array<{
        id: string;
        mac: string;
        host: string;
        ok: boolean;
        state: string;
        reason?: string;
    }>;
    error?: {
        type: string;
        message: string;
    };
};

@Injectable()
export class GeekomPowerService {
    private readonly python =
        process.env.POWERCTL_PYTHON ??
        (process.platform === "win32" ? "python.exe" : "/usr/bin/python3");

    private readonly controller =
        process.env.POWERCTL_SCRIPT ??
        (process.platform === "win32"
            ? "C:\\PowerController\\powerctl.py"
            : "/opt/geekom-power-controller/powerctl.py");

    private readonly config =
        process.env.POWERCTL_CONFIG ??
        (process.platform === "win32"
            ? "C:\\PowerController\\config.json"
            : "/etc/geekom-power-controller/config.json");

    wake(deviceIds: string[] = []): Promise<PowerControllerResult> {
        return this.run("wake", deviceIds);
    }

    status(deviceIds: string[] = []): Promise<PowerControllerResult> {
        return this.run("status", deviceIds);
    }

    shutdown(deviceIds: string[] = []): Promise<PowerControllerResult> {
        return this.run("shutdown", deviceIds);
    }

    private run(action: PowerAction, deviceIds: string[]): Promise<PowerControllerResult> {
        if (deviceIds.some((id) => !/^[a-zA-Z0-9._-]{1,64}$/.test(id))) {
            return Promise.reject(new Error("Invalid device ID"));
        }

        return new Promise((resolve, reject) => {
            const child = spawn(
                this.python,
                [this.controller, "--config", this.config, action, ...deviceIds],
                {
                    shell: false,
                    windowsHide: true,
                    stdio: ["ignore", "pipe", "pipe"],
                },
            );

            let stdout = "";
            let stderr = "";

            const timeout = setTimeout(() => {
                child.kill("SIGKILL");
            }, 180_000);

            child.stdout.setEncoding("utf8");
            child.stderr.setEncoding("utf8");
            child.stdout.on("data", (chunk: string) => stdout += chunk);
            child.stderr.on("data", (chunk: string) => stderr += chunk);

            child.on("error", (error) => {
                clearTimeout(timeout);
                reject(error);
            });

            child.on("close", (processExitCode) => {
                clearTimeout(timeout);
                try {
                    const result = JSON.parse(stdout) as PowerControllerResult;
                    if (result.exitCode !== processExitCode) {
                        reject(new Error(`Exit-code mismatch: process=${processExitCode}, JSON=${result.exitCode}`));
                        return;
                    }
                    resolve(result);
                } catch (error) {
                    reject(new Error(`Invalid power-controller output: ${String(error)}; stderr=${stderr}`));
                }
            });
        });
    }
}
