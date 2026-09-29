export function ProjectShell({ projectId, children }: { projectId: string; children: React.ReactNode }) {
  return (
    <div className="project-shell" data-project-id={projectId}>
      <div className="project-shell-body">{children}</div>
    </div>
  );
}
