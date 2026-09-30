/**
 * El detalle de un escaneo, en un modal, con la forma que le toca.
 *
 * ## Por qué el `kind` es un parámetro y no se deduce del resultado
 *
 * Porque un escaneo **fallido** no tiene resultado, y de un `result` nulo no se puede deducir si
 * era de contenedor o de red. Un detalle que cambiaria de forma segun si el escaneo ha terminado
 * seria una pantalla que se Rearma sola, y en la de red aparecerian cuatro listas de paquetes
 * vacias. El tipo lo sabe la pantalla desde la que se abre el modal, que es quien lo encoló.
 *
 * ## Por qué los límites van **dentro** de la vista y no en un aviso al pie
 *
 * Porque quien lee el detalle de una imagen con 88 paquetes y sin ninguna vulnerabilidad no
 * puede saber que no hay ninguna. Ve un inventario y una columna en blanco, y un inventario en
 * blanco de vulnerabilidades se lee como «está limpio». La tarjeta del límite se pone en el sitio
 * donde la ausencia se nota.
 *
 * ## Por qué los banners se enseñan tal cual
 *
 * Porque son lo que se leyó de la red, sin interpretar. Un banner de PostgreSQL dice
 * `PostgreSQL 13.4`; que esté desactualizado lo decide el razonamiento de la plataforma, que es
 * donde está la clave del proveedor. Recortarlos aquí sería quitar el dato y dejar la forma.
 */

import { useTranslation } from 'react-i18next'

import type { AgentJob, AgentJobKind, ContainerScanResult, NetworkScanResult } from '../../types/agents'
import { useMotivo } from './motivos'
import { StatusBadge } from './StatusBadge'

interface Props {
  job: AgentJob
  kind: AgentJobKind
  onClose: () => void
}

export function JobDetailPanel({ job, kind, onClose }: Props) {
  const { t } = useTranslation('agents')
  const resultado = job.result
  const espacio = kind === 'NETWORK_SCAN' ? 'networks' : 'containers'

  return (
    <div className="modal-backdrop" role="presentation">
      <div
        className="modal modal-wide"
        role="dialog"
        aria-modal="true"
        aria-labelledby="job-detail-title"
      >
        <div className="modal-header">
          <h2 id="job-detail-title" className="mono">
            {job.target}
          </h2>
        </div>

        <div className="modal-body">
          <dl className="detail-grid">
            <dt>{t('common:status')}</dt>
            <dd>
              <StatusBadge job={job} />
            </dd>
            {job.agent_name && (
              <>
                <dt>{t('agent.table.name')}</dt>
                <dd className="mono">{job.agent_name}</dd>
              </>
            )}
            <dt>{t(`${espacio}.table.attempts`)}</dt>
            <dd className="mono">{job.attempt_count}</dd>
            <dt>{t(`${espacio}.table.started`)}</dt>
            <dd>{job.claimed_at ? new Date(job.claimed_at).toLocaleString() : '—'}</dd>
            <dt>{t(`${espacio}.table.evidence`)}</dt>
            <dd>
              {job.evidence_intact ? (
                <span className="badge badge-success">{t('jobs.evidenciaIntact')}</span>
              ) : (
                <span className="badge badge-error" title={t('jobs.evidenciaAlterada')}>
                  {t('jobs.evidenciaAlterada')}
                </span>
              )}
            </dd>
          </dl>

          {job.status === 'QUEUED' && (
            <p className="inline-notice inline-notice-warning" role="status">
              {t(`${espacio}.waitingForAgent`)}
            </p>
          )}
          {(job.status === 'CLAIMED' || job.status === 'RUNNING') && (
            <p className="inline-notice" role="status">
              {t(`${espacio}.inProgress`)}
            </p>
          )}
          {job.error_message && (
            <div className="inline-notice inline-notice-error" role="alert">
              <p className="mono">{job.error_message}</p>
            </div>
          )}

          {job.status === 'COMPLETED' && !resultado && (
            <p className="inline-notice">{t(`${espacio}.noResult`)}</p>
          )}

          {kind === 'CONTAINER_SCAN' && resultado && (
            <ContainerDetail resultado={resultado as unknown as ContainerScanResult} />
          )}
          {kind === 'NETWORK_SCAN' && resultado && (
            <NetworkDetail resultado={resultado as unknown as NetworkScanResult} />
          )}
        </div>

        <div className="modal-footer">
          <button type="button" className="primary-button" onClick={onClose}>
            {t('common:close')}
          </button>
        </div>
      </div>
    </div>
  )
}

function ContainerDetail({ resultado }: { resultado: ContainerScanResult }) {
  const { t } = useTranslation('agents')
  const motivo = useMotivo()
  const paquetes = Array.isArray(resultado.paquetes) ? resultado.paquetes : []
  const sinVulnerabilidades = motivo(
    resultado.codigo_sin_vulnerabilidades_por_paquete,
    resultado.motivo_sin_vulnerabilidades_por_paquete
  )
  const formato = motivo(resultado.codigo_formato_no_leido, resultado.formato_no_leido)
  return (
    <>
      <dl className="detail-grid">
        <dt>{t('containers.table.os')}</dt>
        <dd className="mono">
          {resultado.sistema_operativo}
          {resultado.version_sistema_operativo ? ` ${resultado.version_sistema_operativo}` : ''}
        </dd>
        <dt>{t('containers.table.digest')}</dt>
        <dd className="mono cell-digest">{resultado.digest}</dd>
        <dt>{t('containers.table.reference')}</dt>
        <dd className="mono">{resultado.referencia}</dd>
        <dt>{t('containers.table.layers')}</dt>
        <dd className="mono">
          {resultado.total_capas} · {t('containers.table.size')}:{' '}
          {(resultado.bytes_capas / 1048576).toFixed(1)} MB
        </dd>
      </dl>

      {sinVulnerabilidades && (
        <div className="inline-notice inline-notice-warning">
          <p>
            <strong>{t('limits.vulnerabilitiesTitle')}</strong>
          </p>
          <p>{sinVulnerabilidades}</p>
        </div>
      )}

      {formato && (
        <div className="inline-notice">
          <p>
            <strong>{t('limits.incompleteTitle')}</strong>
          </p>
          <p>{formato}</p>
        </div>
      )}

      <h3 className="modal-subtitle">
        {t('containers.detail.packages', { count: resultado.total_paquetes })}
      </h3>
      {paquetes.length === 0 ? (
        <p className="cell-muted">{t('containers.detail.noPackages')}</p>
      ) : (
        <div className="table-wrapper">
          <table className="data-table">
            <thead>
              <tr>
                <th>{t('containers.detail.package')}</th>
                <th>{t('containers.detail.version')}</th>
                <th>{t('containers.detail.ecosystem')}</th>
              </tr>
            </thead>
            <tbody>
              {paquetes.map((paquete) => (
                <tr key={`${paquete.ecosystem}:${paquete.name}`}>
                  <td className="mono">{paquete.name}</td>
                  <td className="mono">{paquete.version}</td>
                  <td className="mono">{paquete.ecosystem}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  )
}

function NetworkDetail({ resultado }: { resultado: NetworkScanResult }) {
  const { t } = useTranslation('agents')
  const motivo = useMotivo()
  const hosts = Array.isArray(resultado.hosts) ? resultado.hosts : []
  const sinVulnerabilidades = motivo(
    resultado.codigo_sin_vulnerabilidades_por_puerto,
    resultado.motivo_sin_vulnerabilidades_por_puerto
  )
  const recorte = motivo(resultado.codigo_recorte, resultado.motivo_recorte, {
    total: resultado.total_en_el_prefijo,
    maximo: resultado.direcciones_analizadas,
  })
  return (
    <>
      <dl className="detail-grid">
        <dt>{t('networks.table.cidr')}</dt>
        <dd className="mono">{resultado.cidr}</dd>
        <dt>{t('networks.kpis.addresses')}</dt>
        <dd className="mono">{resultado.direcciones_analizadas}</dd>
        <dt>{t('networks.kpis.hosts')}</dt>
        <dd className="mono">{resultado.hosts_con_puertos}</dd>
        <dt>{t('networks.kpis.ports')}</dt>
        <dd className="mono">{resultado.hosts_con_puertos}</dd>
        <dt>{t('networks.table.duration')}</dt>
        <dd className="mono">{resultado.durata_segundos}s</dd>
      </dl>

      {sinVulnerabilidades && (
        <div className="inline-notice inline-notice-warning">
          <p>
            <strong>{t('limits.vulnerabilitiesTitle')}</strong>
          </p>
          <p>{sinVulnerabilidades}</p>
        </div>
      )}

      {recorte && (
        <div className="inline-notice inline-notice-warning">
          <p>{recorte}</p>
        </div>
      )}

      <h3 className="modal-subtitle">{t('networks.detail.hosts', { count: hosts.length })}</h3>
      {hosts.length === 0 ? (
        <p className="cell-muted">{t('networks.detail.noHosts')}</p>
      ) : (
        <div className="table-wrapper">
          <table className="data-table">
            <thead>
              <tr>
                <th>{t('networks.detail.ip')}</th>
                <th>{t('networks.detail.port')}</th>
                <th>{t('networks.detail.service')}</th>
                <th>{t('networks.detail.banner')}</th>
              </tr>
            </thead>
            <tbody>
              {hosts.map((host) => (
                <tr key={host.ip}>
                  <td className="mono">{host.ip}</td>
                  <td className="mono">{host.puertos.map((p) => p.puerto).join(', ')}</td>
                  <td className="mono">
                    {host.puertos.map((p) => p.servicio ?? '—').join(', ')}
                  </td>
                  <td className="mono cell-banner">
                    {host.puertos
                      .map((p) => p.banner)
                      .filter(Boolean)
                      .join(' | ') || '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  )
}
