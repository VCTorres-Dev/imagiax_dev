import { Types } from '@ohif/core';

const PANEL_ID = '@ohif/extension-imagiax-ai.panelModule.imagiaxAiPanel';

const getCommandsModule = ({
  servicesManager,
}: Types.Extensions.ExtensionParams): Types.Extensions.CommandsModule => {
  const { panelService } = servicesManager.services;

  const actions = {
    toggleImagiaxPanel: () => {
      panelService.activatePanel(PANEL_ID, true);
    },
  };

  const definitions = {
    toggleImagiaxPanel: actions.toggleImagiaxPanel,
  };

  return {
    actions,
    definitions,
    defaultContext: 'DEFAULT',
  };
};

export default getCommandsModule;
