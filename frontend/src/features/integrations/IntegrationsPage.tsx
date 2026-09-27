import { useMemo } from 'react'
import { useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import {
  ArrowUpRight,
  Bell,
  Boxes,
  Bug,
  Check,
  Cloud,
  Code2,
  MessageSquare,
} from 'lucide-react'

import { useAuth } from '../auth/useAuth'
import { useDashboardSummary } from '../dashboard/useDashboardSummary'

/**
 * Pagina de integraciones.
 *
 * ## Por qué cuatro categorías y no una lista plana
 *
 * Porque "integraciones" no es un tipo de cosa. Conectar un proveedor de código, un canal de
 * notificación, un gestor de incidencias y una nube son cuatro operaciones distintas, con
 * permisos distintos y con consecuencias distintas cuando fallan: una credencial de nube puede
 * costar dinero, un canal de notificación solo pierde avisos. Mezclarlas en una lista alfabética
 * hace que el usuario tenga que acordarse de qué es cada cosa.
 *
 * ## Por qué hay entradas que no se pueden conectar todavía
 *
 * Porque el botón de una integración sin persistencia detrás es peor que no tenerlo. Un
 * formulario de credenciales que no guarda nada se ve terminado hasta que alguien pega un
 * token de verdad y descubre que se ha perdido, y ese momento es en producción, no en la demo.
 *
 * Por eso las que no tienen destino real llevan etiqueta de estado y el motivo, en vez de un
 * botón que abre un diálogo decorativo. La excepción es GitHub, GitLab, Bitbucket y Gitea: esas
 * sí se guardan, en `git_credentials`, y por eso abren el alta real de repositorio.
 */
export function IntegrationsPage() {
  const { t } = useTranslation('integrations')
  const navigate = useNavigate()
  const { selectedOrganizationId } = useAuth()
  const { summary, isLoading: cargando, loadFailed: error } = useDashboardSummary()

  const repositorios = useMemo(() => summary?.repositories ?? [], [summary])

  /**
   * Que proveedores tienen ya al menos un repositorio conectado.
   *
   * Se deriva del recuento y no de una consulta aparte: el resumen del panel ya trae la
   * lista, y pedirla otra vez seria una segunda peticion de lo mismo que puede discrepar de
   * la primera durante un refresco. La pagina de Integraciones mostraria «GitHub sin
   * conectar» al lado de un panel que muestra dos repositorios de GitHub.
   */
  const conectados = useMemo(() => {
    const porProveedor = new Set<string>()
    for (const repo of repositorios) {
      if (repo.provider) {
        porProveedor.add(repo.provider)
      }
    }
    return porProveedor
  }, [repositorios])

  const totalRepos = repositorios.length

  /**
   * Los proveedores de código, agrupados.
   *
   * Se declara como dato porque la tabla de configuracion por proveedor es lo unico que varia
   * entre ellos, y repetir el JSX cuatro veces haria que añadir GitLab+Gitea —que ya estan
   * soportados por el backend— fuera copiar y pegar.
   */
  const codeProviders = useMemo(
    () =>
      (
        [
          {
            id: 'GITHUB',
            nombre: 'GitHub',
            descripcion: 'Revisiones de pull request y escaneo automático en cada merge.',
          },
          {
            id: 'GITLAB',
            nombre: 'GitLab',
            descripcion: 'Proyectos de GitLab con revisión de merge request.',
          },
          {
            id: 'BITBUCKET',
            nombre: 'Bitbucket',
            descripcion: 'Repositorios de Bitbucket con webhooks de cambio.',
          },
          {
            id: 'GITEA',
            nombre: 'Gitea',
            descripcion: 'Instancias de Gitea autoalojadas.',
          },
        ] as const
      ).map((proveedor) => ({
        ...proveedor,
        conectado: conectados.has(proveedor.id),
      })),
    [conectados],
  )

  return (
    <section className="page-section" aria-labelledby="integrations-title">
      <div className="page-header">
        <div>
          <p className="eyebrow">{t('eyebrow')}</p>
          <h1 id="integrations-title">{t('title')}</h1>
          <p className="page-description">{t('description')}</p>
        </div>
      </div>

      {selectedOrganizationId === null ? null : (
        <p className="inline-notice inline-notice-warning" role="status">
          {t('noWorkspace')}
        </p>
      )}

      {error ? (
        <div className="empty-card">
          <p>{t('states.error')}</p>
          <button
            className="secondary-button"
            type="button"
            onClick={() => navigate('/integrations')}
          >
            <span>{t('states.retry')}</span>
          </button>
        </div>
      ) : null}

      {/* ---------------------------------------------------------------- */}
      {/* Proveedores de código                                            */}
      {/* ---------------------------------------------------------------- */}
      <section className="panel" aria-labelledby="integrations-code">
        <div className="settings-section-heading">
          <h2 id="integrations-code">
            <Code2 size={17} aria-hidden="true" />
            {t('code.title')}
          </h2>
          <p>{t('code.description')}</p>
        </div>

        {cargando ? (
          <p className="chart-empty">{t('states.loading')}</p>
        ) : (
          <div className="integration-grid">
            {codeProviders.map((proveedor) => (
              <article key={proveedor.id} className="integration-card">
                <div className="integration-copy">
                  <h3 className="integration-name">{proveedor.nombre}</h3>
                  <p className="integration-note">{t(`code.providers.${proveedor.id}.note`)}</p>
                </div>
                {proveedor.conectado ? (
                  <span className="integration-state integration-state-on">
                    <Check size={13} aria-hidden="true" />
                    {t('states.connected')}
                  </span>
                ) : (
                  <span className="integration-state integration-state-off">
                    {t('states.notConnected')}
                  </span>
                )}
                <button
                  className={proveedor.conectado ? 'ghost-button' : 'secondary-button'}
                  type="button"
                  onClick={() => navigate('/repositories')}
                >
                  <span>
                    {proveedor.conectado
                      ? t('actions.configure')
                      : t('actions.connect')}
                  </span>
                  <ArrowUpRight size={15} aria-hidden="true" />
                </button>
              </article>
            ))}
          </div>
        )}

        {totalRepos > 0 ? (
          <p className="integration-footnote">
            {t('code.repositoriesConnected', { count: totalRepos })}
          </p>
        ) : null}
      </section>

      {/* ---------------------------------------------------------------- */}
      {/* Notificaciones                                                    */}
      {/* ---------------------------------------------------------------- */}
      <section className="panel" aria-labelledby="integrations-notifications">
        <div className="settings-section-heading">
          <h2 id="integrations-notifications">
            <Bell size={17} aria-hidden="true" />
            {t('notifications.title')}
          </h2>
          <p>{t('notifications.description')}</p>
        </div>
        <div className="integration-grid">
          {(
            [
              { id: 'slack', nombre: 'Slack' },
              { id: 'teams', nombre: 'Microsoft Teams' },
            ] as const
          ).map((destino) => (
            <article key={destino.id} className="integration-card">
              <div className="integration-copy">
                <h3 className="integration-name">{destino.nombre}</h3>
                <p className="integration-note">
                  {t(`notifications.destinations.${destino.id}.note`)}
                </p>
              </div>
              <span className="integration-state integration-state-off">
                {t('states.comingSoon')}
              </span>
              <p className="integration-pending">{t('notifications.pendingReason')}</p>
            </article>
          ))}
        </div>
      </section>

      {/* ---------------------------------------------------------------- */}
      {/* Seguimiento de incidencias                                       */}
      {/* ---------------------------------------------------------------- */}
      <section className="panel" aria-labelledby="integrations-issues">
        <div className="settings-section-heading">
          <h2 id="integrations-issues">
            <Bug size={17} aria-hidden="true" />
            {t('issues.title')}
          </h2>
          <p>{t('issues.description')}</p>
        </div>
        <div className="integration-grid">
          {(
            [
              { id: 'jira', nombre: 'Jira' },
              { id: 'linear', nombre: 'Linear' },
            ] as const
          ).map((destino) => (
            <article key={destino.id} className="integration-card">
              <div className="integration-copy">
                <h3 className="integration-name">{destino.nombre}</h3>
                <p className="integration-note">{t(`issues.destinations.${destino.id}.note`)}</p>
              </div>
              <span className="integration-state integration-state-off">
                {t('states.comingSoon')}
              </span>
              <p className="integration-pending">{t('issues.pendingReason')}</p>
            </article>
          ))}
        </div>
      </section>

      {/* ---------------------------------------------------------------- */}
      {/* Infraestructura                                                  */}
      {/* ---------------------------------------------------------------- */}
      <section className="panel" aria-labelledby="integrations-infrastructure">
        <div className="settings-section-heading">
          <h2 id="integrations-infrastructure">
            <Cloud size={17} aria-hidden="true" />
            {t('infrastructure.title')}
          </h2>
          <p>{t('infrastructure.description')}</p>
        </div>
        <div className="integration-grid">
          {(
            [
              { id: 'aws', nombre: 'AWS' },
              { id: 'vercel', nombre: 'Vercel' },
              { id: 'supabase', nombre: 'Supabase' },
              { id: 'cloudflare', nombre: 'Cloudflare' },
              { id: 'gcp', nombre: 'Google Cloud' },
              { id: 'railway', nombre: 'Railway' },
            ] as const
          ).map((destino) => (
            <article key={destino.id} className="integration-card">
              <div className="integration-copy">
                <h3 className="integration-name">{destino.nombre}</h3>
                <p className="integration-note">
                  {t(`infrastructure.destinations.${destino.id}.note`)}
                </p>
              </div>
              <span className="integration-state integration-state-off">
                {t('states.comingSoon')}
              </span>
            </article>
          ))}
        </div>

        {/*
          Este boton si tiene destino real, y por eso es el unico de la pagina que abre algo
          en vez de marcar el estado: el servidor MCP se configura con un token que ya existe,
          en la pestana de API Access. Enlazar ahi y no a un formulario nuevo evita que el
          usuario tenga que volver a escribir la credencial que acaba de crear.
        */}
        <div className="integration-cta">
          <Boxes size={17} aria-hidden="true" />
          <div>
            <h3 className="integration-name">{t('infrastructure.mcpTitle')}</h3>
            <p className="integration-note">{t('infrastructure.mcpNote')}</p>
          </div>
          <button
            className="primary-button"
            type="button"
            onClick={() => navigate('/api-access')}
          >
            <MessageSquare size={15} aria-hidden="true" />
            <span>{t('actions.addMcpServer')}</span>
          </button>
        </div>
      </section>
    </section>
  )
}
