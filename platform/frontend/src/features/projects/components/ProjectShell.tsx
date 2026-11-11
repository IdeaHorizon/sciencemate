"use client";

import { useProjects } from "../hooks/useProjects";
import { RemoteConnectionStatus } from "./RemoteConnectionStatus";

export function ProjectShell({ projectId, children }: { projectId: string; children: React.ReactNode }) {
  const projects = useProjects();
  const home = projects.data?.find((project) => project.id === projectId)?.home;
  return (
    <div className="project-shell" data-project-id={projectId}>
      {home?.kind === "remote" && <RemoteConnectionStatus key={home.connection_id}
        projectId={projectId} serverId={home.connection_id} name={home.name} />}
      <div className="project-shell-body">{children}</div>
    </div>
  );
}
