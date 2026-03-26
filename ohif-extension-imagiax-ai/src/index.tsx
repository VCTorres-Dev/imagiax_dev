import { Types } from '@ohif/core';

import getPanelModule from './getPanelModule';
import getCommandsModule from './getCommandsModule';
import { id } from './id';

const imagiaxAiExtension: Types.Extensions.Extension = {
  id,
  getPanelModule,
  getCommandsModule,
};

export default imagiaxAiExtension;
