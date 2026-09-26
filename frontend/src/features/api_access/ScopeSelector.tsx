import { useMemo, useState } from 'react'
import { ChevronDown, ChevronRight, ShieldAlert } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import type { ApiScopeCatalog, ApiScopeDefinition } from '../../types/api'

export interface ScopeSelectorProps {
  catalog: ApiScopeCatalog
  selected: ReadonlySet<string>
  onChange: (next: Set<string>) => void
  isDisabled?: boolean
}

/**
 * Selector de los 46 scopes agrupados por recurso.
 *
 * ## Por qué los grupos son plegables y no una lista plana
 *
 * Una lista de 46 casillas sin agrupar es una tabla deKill screen: el usuario tendría que
 * leer 46 líneas para encontrar `pentests:create`. Con 16 grupos plegables ve primero la
 * forma del sistema de permisos, y abre solo el grupo que le interesa.
 *
 * ## Por qué el permiso privilegiado se marca
 *
 * Los scopes con `is_privileged` vienen marcados por el backend, no se deducen aquí. Un
 * token con `admin:models_manage` o `billing:ledger_read` no es un token más: cambia lo
 * que la plataforma considera fiable para operar. Marcarlo es la diferencia entre
 * que el usuario lo lea en el momento o que se entere en la auditoría.
 */
export function ScopeSelector({
  catalog,
  selected,
  onChange,
  isDisabled = false,
}: ScopeSelectorProps) {
  const { t } = useTranslation('apiAccess')
  const [collapsed, setCollapsed] = useState<ReadonlySet<string>>(new Set())

  const total = catalog.total
  const chosen = selected.size

  const toggle = (scope: string) => {
    const next = new Set(selected)
    if (next.has(scope)) {
      next.delete(scope)
    } else {
      next.add(scope)
    }
    onChange(next)
  }

  const toggleGroup = (scopes: ApiScopeDefinition[]) => {
    const next = new Set(selected)
    const allChosen = scopes.every((item) => next.has(item.scope))
    for (const item of scopes) {
      if (allChosen) {
        next.delete(item.scope)
      } else {
        next.add(item.scope)
      }
    }
    onChange(next)
  }

  const setAll = () => {
    onChange(chosen === total ? new Set() : new Set(catalog.groups.flatMap((g) => g.scopes.map((s) => s.scope))))
  }

  const toggleCollapsed = (group: string) => {
    const next = new Set(collapsed)
    if (next.has(group)) {
      next.delete(group)
    } else {
      next.add(group)
    }
    setCollapsed(next)
  }

  const privilegedChosen = useMemo(
    () =>
      catalog.groups
        .flatMap((group) => group.scopes)
        .filter((item) => item.is_privileged && selected.has(item.scope)).length,
    [catalog, selected],
  )

  return (
    <div className="scope-selector">
      <header className="scope-selector-header">
        <div className="scope-counter">
          <span className="scope-counter-value mono">
            {chosen} / {total}
          </span>
          <span className="scope-counter-label">{t('scopes.selectedOf', { chosen, total })}</span>
        </div>
        <div>
          <button
            className="link-button"
            type="button"
            disabled={isDisabled || total === 0}
            onClick={setAll}
          >
            {t(chosen === total ? 'scopes.clearAll' : 'scopes.selectAll')}
          </button>
        </div>
      </header>

      {/*
        El aviso de permisos privilegiados va **encima** de la lista, no al lado. Su
        motivo es que se lea antes de marcar la casilla: un aviso que aparece debajo de
        46 casillas ya se ha leído después de marcarlas, que es cuando ya no sirve.
      */}
      {privilegedChosen > 0 && (
        <p className="scope-warning" role="status">
          <ShieldAlert size={16} aria-hidden="true" />
          <span>{t('scopes.privilegedWarning', { count: privilegedChosen })}</span>
        </p>
      )}

      <ul className="scope-groups">
        {catalog.groups.map((group) => {
          const isCollapsed = collapsed.has(group.group)
          const groupSelected = group.scopes.filter((item) => selected.has(item.scope)).length
          const allChosen = group.scopes.every((item) => selected.has(item.scope))
          return (
            <li key={group.group} className="scope-group">
              <div className="scope-group-header">
                <button
                  className="scope-group-toggle"
                  type="button"
                  aria-expanded={!isCollapsed}
                  disabled={isDisabled}
                  onClick={() => toggleCollapsed(group.group)}
                >
                  {isCollapsed ? (
                    <ChevronRight size={16} aria-hidden="true" />
                  ) : (
                    <ChevronDown size={16} aria-hidden="true" />
                  )}
                  <span>{group.group}</span>
                </button>
                <span className="scope-group-count mono">
                  {groupSelected}/{group.scopes.length}
                </span>
                <button
                  className="link-button"
                  type="button"
                  disabled={isDisabled}
                  onClick={() => toggleGroup(group.scopes)}
                >
                  {t(allChosen ? 'scopes.clearGroup' : 'scopes.selectGroup')}
                </button>
              </div>
              {!isCollapsed && (
                <ul className="scope-list">
                  {group.scopes.map((item) => (
                    <li key={item.scope}>
                      <label className="scope-item">
                        <input
                          type="checkbox"
                          checked={selected.has(item.scope)}
                          disabled={isDisabled}
                          onChange={() => toggle(item.scope)}
                        />
                        <span className="scope-item-text">
                          <span className="scope-item-label">
                            {t(`scopes.actions.${item.action}`, {
                              defaultValue: item.action,
                            })}
                          </span>
                          <span className="scope-item-value mono">{item.scope}</span>
                        </span>
                        {item.is_privileged && (
                          <span
                            className="scope-item-flag"
                            title={t('scopes.privilegedShort')}
                            aria-label={t('scopes.privilegedShort')}
                          >
                            *
                          </span>
                        )}
                      </label>
                    </li>
                  ))}
                </ul>
              )}
            </li>
          )
        })}
      </ul>
    </div>
  )
}
