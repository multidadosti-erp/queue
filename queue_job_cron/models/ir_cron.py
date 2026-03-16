# Copyright 2019 ACSONE SA/NV (<http://acsone.eu>)
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl.html)
import logging
from odoo import api, fields, models
from odoo.addons.queue_job.job import job

_logger = logging.getLogger(__name__)


class IrCron(models.Model):
    """Extende o agendador para opcionalmente executar crons na fila.

    Quando ``run_as_queue_job`` esta ativo, o cron deixa de executar de forma
    imediata e passa a gerar um ``queue.job``. Tambem aplica uma regra de
    deduplicacao para evitar acumulo de jobs equivalentes ainda ativos.
    """

    _inherit = 'ir.cron'

    _queue_job_active_states = ('pending', 'enqueued', 'started')

    run_as_queue_job = fields.Boolean(help="Specify if this cron should be "
                                           "ran as a queue job")
    channel_id = fields.Many2one(comodel_name='queue.job.channel',
                                 string='Channel')

    @api.onchange('run_as_queue_job')
    def onchange_run_as_queue_job(self):
        """Preenche automaticamente o canal padrao ao ativar fila no cron."""
        for cron in self:
            if cron.run_as_queue_job and not cron.channel_id:
                cron.channel_id = self.env.ref(
                    'queue_job_cron.channel_root_ir_cron').id

    @job(default_channel='root.ir_cron')
    @api.model
    def _run_job_as_queue_job(self, server_action):
        """Executa a server action dentro do worker da fila."""
        return server_action.run()

    @api.multi
    def _queue_job_identity_key(self, server_action):
        """Monta a chave de identidade unica do job para este cron/acao.

        A chave e usada pelo ``queue_job`` para identificar jobs equivalentes
        e impedir enfileiramentos duplicados enquanto houver execucao ativa.
        """
        self.ensure_one()
        return 'ir_cron:%s:server_action:%s' % (self.id, server_action.id)

    @api.multi
    def _has_active_queue_job(self, server_action):
        """Informa se ja existe job ativo do mesmo cron/acao na fila.

        Considera como ativo os estados ``pending``, ``enqueued`` e
        ``started``.
        """
        self.ensure_one()
        identity_key = self._queue_job_identity_key(server_action)
        return bool(self.env['queue.job'].sudo().search_count([
            ('identity_key', '=', identity_key),
            ('state', 'in', self._queue_job_active_states),
        ]))

    @api.multi
    def _enqueue_cron_as_queue_job(self, server_action):
        """Enfileira o cron como job assincrono com protecao contra duplicacao.

        Se encontrar um job equivalente ativo, nao cria outro registro.
        """
        self.ensure_one()
        if self._has_active_queue_job(server_action):
            _logger.info(
                'Skipping queue job for cron %s (%s) because an active job is already queued/running.',
                self.name,
                self.id,
            )
            return False
        return self.sudo(user=self.user_id.id).with_delay(
            priority=self.priority,
            description=self.name,
            channel=self.channel_id.complete_name,
            identity_key=self._queue_job_identity_key(server_action),
        )._run_job_as_queue_job(
            server_action=server_action.sudo(self.user_id.id)
        )

    @api.multi
    def method_direct_trigger(self):
        """Intercepta disparo manual do cron para enviar para fila quando ativo.

        Mantem o comportamento original quando ``run_as_queue_job`` estiver
        desativado.
        """
        self.check_access_rights('write')
        if self.run_as_queue_job:
            return self._enqueue_cron_as_queue_job(self.ir_actions_server_id)
        else:
            return super(IrCron, self).method_direct_trigger()

    @api.model
    def _callback(self, cron_name, server_action_id, job_id):
        """Intercepta execucao automatica do scheduler para fila quando ativo.

        O scheduler chama este callback; quando o cron esta configurado para
        fila, o modulo cria (ou reaproveita) o job assincrono correspondente.
        """
        cron = self.env['ir.cron'].sudo().browse(job_id)
        if cron.run_as_queue_job:
            server_action = self.env['ir.actions.server'].browse(
                server_action_id)
            return cron._enqueue_cron_as_queue_job(server_action)
        else:
            return super(IrCron, self)._callback(
                cron_name=cron_name,
                server_action_id=server_action_id,
                job_id=job_id)
