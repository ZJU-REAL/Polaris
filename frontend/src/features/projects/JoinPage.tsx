import { useNavigate, useParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { toast } from '../../components/ui/Toast';
import { useProject } from '../../app/project';
import { api } from '../../lib/api';
import { tr } from '../../lib/i18n';

/* /join/:token — 邀请链接落地页：预览方向信息并加入。 */

export function JoinPage() {
  const { token = '' } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { setCurrentProjectId } = useProject();

  const { data: info, isLoading, isError } = useQuery({
    queryKey: ['invite-info', token],
    queryFn: () => api.inviteInfo(token),
    retry: false,
    enabled: !!token,
  });

  const acceptMutation = useMutation({
    mutationFn: () => api.acceptInvite(token),
    onSuccess: (project) => {
      toast(`${tr('已加入研究方向：', 'Joined research direction: ')}${project.name}`, 'ok');
      void queryClient.invalidateQueries({ queryKey: ['projects'] });
      setCurrentProjectId(project.id);
      navigate(`/projects/${project.id}`);
    },
    onError: (e) => {
      const msg = e instanceof Error ? e.message : String(e);
      toast(msg === 'INVITE_INVALID' ? tr('邀请链接已失效', 'Invite link is no longer valid') : `${tr('加入失败：', 'Failed to join: ')}${msg}`, 'error');
    },
  });

  return (
    <div className="page fadeup">
      <div className="card card-pad" style={{ maxWidth: 480, margin: '80px auto 0', textAlign: 'center', padding: '48px 36px' }}>
        <div
          style={{
            width: 52, height: 52, borderRadius: 14, margin: '0 auto 18px',
            background: 'var(--accent-soft)', color: 'var(--accent)',
            display: 'flex', alignItems: 'center', justifyContent: 'center',
          }}
        >
          <Icon name="users" size={24} />
        </div>
        {isLoading ? (
          <div style={{ fontSize: 13, color: 'var(--text-2)' }}>{tr('正在验证邀请链接…', 'Verifying invite link…')}</div>
        ) : isError || !info ? (
          <>
            <div style={{ fontSize: 16, fontWeight: 680, marginBottom: 8 }}>{tr('邀请链接无效', 'Invalid invite link')}</div>
            <div style={{ fontSize: 13, color: 'var(--text-2)', marginBottom: 20 }}>{tr('链接不存在或已被撤销，请向邀请人索取新链接。', 'This link does not exist or was revoked. Ask the inviter for a new one.')}</div>
            <button className="btn btn-ghost" onClick={() => navigate('/')}>{tr('返回总览', 'Back to dashboard')}</button>
          </>
        ) : info.already_member ? (
          <>
            <div style={{ fontSize: 16, fontWeight: 680, marginBottom: 8 }}>{tr('你已是该方向成员', 'You are already a member')}</div>
            <div style={{ fontSize: 13, color: 'var(--text-2)', marginBottom: 20 }}>{info.project_name}</div>
            <button className="btn btn-primary" onClick={() => navigate(`/projects/${info.project_id}`)}>{tr('进入方向', 'Open direction')}</button>
          </>
        ) : !info.valid ? (
          <>
            <div style={{ fontSize: 16, fontWeight: 680, marginBottom: 8 }}>{tr('邀请链接已失效', 'Invite link expired')}</div>
            <div style={{ fontSize: 13, color: 'var(--text-2)', marginBottom: 20 }}>{tr('链接已过期或使用次数用尽，请向邀请人索取新链接。', 'This link has expired or hit its usage limit. Ask the inviter for a new one.')}</div>
            <button className="btn btn-ghost" onClick={() => navigate('/')}>{tr('返回总览', 'Back to dashboard')}</button>
          </>
        ) : (
          <>
            <div style={{ fontSize: 16, fontWeight: 680, marginBottom: 8 }}>{tr('邀请你加入研究方向', 'You are invited to join a research direction')}</div>
            <div style={{ fontSize: 14, color: 'var(--text)', marginBottom: 4 }}>{info.project_name}</div>
            {info.inviter_name && (
              <div style={{ fontSize: 12, color: 'var(--text-3)', marginBottom: 20 }}>{tr('邀请人：', 'Invited by: ')}{info.inviter_name}</div>
            )}
            <button
              className="btn btn-primary"
              disabled={acceptMutation.isPending}
              onClick={() => acceptMutation.mutate()}
            >
              {acceptMutation.isPending ? tr('加入中…', 'Joining…') : tr('加入该方向', 'Join this direction')}
            </button>
          </>
        )}
      </div>
    </div>
  );
}
