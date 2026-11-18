import { useProject } from '../../app/project';
import { ProjectSettings } from '../projects/ProjectDetailPage';
import { TOPIC_TASK_KINDS, VoyagesList } from '../voyages/VoyagesPage';

/* 课题页头下的两页：这个课题的任务、这个课题的设置。
   以前是「课题工作台」里的两个标签（/t/:id?tab=tasks|settings），现在各有自己的地址。 */

/** /t/:id/tasks */
export function TopicTasksPage() {
  return (
    <div className="page fadeup">
      <VoyagesList kinds={TOPIC_TASK_KINDS} />
    </div>
  );
}

/** /t/:id/settings */
export function TopicSettingsPage() {
  const { currentProjectId } = useProject();
  if (!currentProjectId) return null;
  return (
    <div className="page fadeup">
      <ProjectSettings id={currentProjectId} embedded />
    </div>
  );
}
