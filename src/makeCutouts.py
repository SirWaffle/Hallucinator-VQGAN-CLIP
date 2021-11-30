import imageUtils

import torch
from torch.cuda.amp import autocast
from torch import nn
from torch.nn import functional as F
from torch.cuda.amp import custom_fwd
from torch.cuda.amp import custom_bwd

import kornia.augmentation as K

# hack to manage mixed_precision
# set from generate based on cmd args
use_mixed_precision = False
deterministic = False


###################
##  string based switch statement 'factory'
###################
def GetMakeCutouts( cutMethod:str, clipPerceptorInputResolution:int, cutNum:int, cutSize, cutPow:float, augments:list ):
    # Cutout class options:
    # 'squish', 'latest','original','updated' or 'updatedpooling'
    if cutMethod == 'latest':
        cutMethod = "nerdy"

    if cutSize[0] == 0:
        cutSize[0] = clipPerceptorInputResolution

    if cutSize[1] == 0:
        cutSize[1] = clipPerceptorInputResolution    

    cutsMatchClip = True 
    if clipPerceptorInputResolution != cutSize or clipPerceptorInputResolution != cutSize[0] or clipPerceptorInputResolution != cutSize[1]:
        cutsMatchClip = False

    print("Cutouts method: " + cutMethod + " using cutSize: " + str(cutSize) + '  Matches clipres: ' + str(cutsMatchClip))

    # used for whatever test cut thing im doing
    if cutMethod == 'test':
        make_cutouts = MakeCutoutsOneSpot(clipPerceptorInputResolution, cutSize[0], cutSize[1], cutNum, cut_pow=cutPow, use_pool=True, augments=augments)

    elif cutMethod == 'maskTest':
        make_cutouts = MakeCutoutsMaskTest(clipPerceptorInputResolution, cutSize[0], cutSize[1], cutNum, cut_pow=cutPow, use_pool=False, augments=[])

    elif cutMethod == 'growFromCenter':
        make_cutouts = MakeCutoutsGrowFromCenter(clipPerceptorInputResolution, cutSize[0], cutSize[1], cutNum, cut_pow=cutPow, use_pool=True, augments=augments)        
    elif cutMethod == 'squish':        
        make_cutouts = MakeCutoutsSquish(clipPerceptorInputResolution, cutSize[0], cutSize[1], cutNum, cut_pow=cutPow, use_pool=True, augments=augments)
    elif cutMethod == 'original':
        make_cutouts = MakeCutoutsOrig(clipPerceptorInputResolution, cutNum, cut_pow=cutPow, augments=augments)
    elif cutMethod == 'nerdy':
        make_cutouts = MakeCutoutsNerdy(clipPerceptorInputResolution, cutNum, cut_pow=cutPow, augments=augments)
    elif cutMethod == 'nerdyNoPool':
        make_cutouts = MakeCutoutsNerdyNoPool(clipPerceptorInputResolution, cutNum, cut_pow=cutPow, augments=augments)
    else:
        print("Bad cut method selected")

    return make_cutouts



class ClampWithGrad(torch.autograd.Function):
    @staticmethod
    @custom_fwd
    def forward(ctx, input, min, max):
        ctx.min = min
        ctx.max = max
        ctx.save_for_backward(input)
        return input.clamp(min, max)

    @staticmethod
    @custom_bwd
    def backward(ctx, grad_in):
        input, = ctx.saved_tensors
        return grad_in * (grad_in * (input - input.clamp(ctx.min, ctx.max)) >= 0), None, None

clamp_with_grad = ClampWithGrad.apply



def setupAugmentList(augments, cut_size_x, cut_size_y):
    # Pick your own augments & their order
    augment_list = []
    if augments:
        for item in augments[0]:
            if item == 'Ji':
                augment_list.append(K.ColorJitter(brightness=0.1, contrast=0.1, saturation=0.1, hue=0.1, p=0.7))
            elif item == 'Sh':
                augment_list.append(K.RandomSharpness(sharpness=0.3, p=0.5))
            elif item == 'Gn':
                augment_list.append(K.RandomGaussianNoise(mean=0.0, std=1., p=0.5))
            elif item == 'Pe':
                augment_list.append(K.RandomPerspective(distortion_scale=0.7, p=0.7))
            elif item == 'Ro':
                augment_list.append(K.RandomRotation(degrees=15, p=0.7))
            elif item == 'Af':
                augment_list.append(K.RandomAffine(degrees=15, translate=0.1, shear=5, p=0.7, padding_mode='zeros', keepdim=True)) # border, reflection, zeros
            elif item == 'Et':
                augment_list.append(K.RandomElasticTransform(p=0.7))
            elif item == 'Ts':
                augment_list.append(K.RandomThinPlateSpline(scale=0.8, same_on_batch=True, p=0.7))
            elif item == 'Cr':
                augment_list.append(K.RandomCrop(size=(cut_size_x,cut_size_y), pad_if_needed=True, padding_mode='reflect', p=0.5))
            elif item == 'Er':
                augment_list.append(K.RandomErasing(scale=(.1, .4), ratio=(.3, 1/.3), same_on_batch=True, p=0.7))
            elif item == 'Re':
                augment_list.append(K.RandomResizedCrop(size=(cut_size_x,cut_size_y), scale=(0.1,1),  ratio=(0.75,1.333), cropping_mode='resample', p=0.5))

    print("augment list: " + str(augment_list))
    return nn.Sequential(*augment_list)   


# modifiable pool / combo with original to create more detail in larger images
# no idea what im doing, still learning, but this looked cool enough to me on images > 1200x1200
# squish
class MakeCutoutsSquish(nn.Module):
    def __init__(self, clipRes, cut_size_x, cut_size_y, cutn, cut_pow=1., use_pool=True, augments=[]):
        super().__init__()
        self.cut_size_x = cut_size_x
        self.cut_size_y = cut_size_y
        self.cutn = cutn
        self.cut_pow = cut_pow # not used with pooling
        self.use_pool = use_pool
        self.clipRes = clipRes
        
        #self.augs = setupAugmentList(cut_size_x, cut_size_y)
        self.augs = setupAugmentList(augments, self.clipRes, self.clipRes)

        self.noise_fac = 0.1
        # self.noise_fac = False

        # Pooling
        self.av_pool = nn.AdaptiveAvgPool2d((self.clipRes, self.clipRes))
        self.max_pool = nn.AdaptiveMaxPool2d((self.clipRes, self.clipRes))

    @autocast(enabled=use_mixed_precision)
    def forward(self, input):
        sideY, sideX = input.shape[2:4]

        max_size_x = sideX
        max_size_y = sideY

        min_size_x = min(sideX, self.cut_size_x)
        min_size_y = min(sideX, self.cut_size_y)

        cutouts = []
        
        for _ in range(self.cutn):            
            # Use Pooling and original method together

            size_x = int(torch.rand([])**self.cut_pow * (max_size_x - min_size_x) + min_size_x)
            size_y = int(torch.rand([])**self.cut_pow * (max_size_y - min_size_y) + min_size_y)

            offsetx = torch.randint(0, sideX - size_x + 1, ())
            offsety = torch.randint(0, sideY - size_y + 1, ())

            cutout = input[:, :, offsety:offsety + size_y, offsetx:offsetx + size_x]

            # now pool for some reason? dont know what i'm doing but the results are good...
            if self.use_pool:
                cutout = (self.av_pool(cutout) + self.max_pool(cutout))/2
                cutouts.append(cutout)
            else:
                cutouts.append(imageUtils.resample(cutout, (self.clipRes, self.clipRes), deterministic=deterministic))            
            
        batch = self.augs(torch.cat(cutouts, dim=0))
        
        if self.noise_fac:
            facs = batch.new_empty([self.cutn, 1, 1, 1]).uniform_(0, self.noise_fac)
            batch = batch + facs * torch.randn_like(batch)

        return batch, [] #TODO, make this return cordinates for masking a well


#latest make cutouts this came with - works well on images <= 600x600 ish
# i belive the pooling like this causes a uniform distribution of squares of cutsize
# nerdy
class MakeCutoutsNerdy(nn.Module):
    def __init__(self, cut_size, cutn, cut_pow=1., augments=[]):
        super().__init__()
        self.cut_size = cut_size
        self.cutn = cutn
        self.cut_pow = cut_pow # not used with pooling
        self.augs = setupAugmentList(augments, cut_size, cut_size)

        self.noise_fac = 0.1
        # self.noise_fac = False

        
        # Pooling
        self.av_pool = nn.AdaptiveAvgPool2d((self.cut_size, self.cut_size))
        self.max_pool = nn.AdaptiveMaxPool2d((self.cut_size, self.cut_size))

    @autocast(enabled=use_mixed_precision)
    def forward(self, input):
        cutouts = []
        
        for _ in range(self.cutn):            
            # Use Pooling
            cutout = (self.av_pool(input) + self.max_pool(input))/2
            cutouts.append(cutout)
            
        batch = self.augs(torch.cat(cutouts, dim=0))
        
        if self.noise_fac:
            facs = batch.new_empty([self.cutn, 1, 1, 1]).uniform_(0, self.noise_fac)
            batch = batch + facs * torch.randn_like(batch)

        return batch, [] #TODO, make this return cordinates for masking a well

# An Nerdy updated version with selectable Kornia augments, but no pooling:
# nerdyNoPool
class MakeCutoutsNerdyNoPool(nn.Module):
    def __init__(self, cut_size, cutn, cut_pow=1., augments=[]):
        super().__init__()
        self.cut_size = cut_size
        self.cutn = cutn
        self.cut_pow = cut_pow
        self.noise_fac = 0.1
        
        # Pick your own augments & their order
        augment_list = []
        for item in augments[0]:
            if item == 'Ji':
                augment_list.append(K.ColorJitter(brightness=0.1, contrast=0.1, saturation=0.1, hue=0.1, p=0.7))
            elif item == 'Sh':
                augment_list.append(K.RandomSharpness(sharpness=0.3, p=0.5))
            elif item == 'Gn':
                augment_list.append(K.RandomGaussianNoise(mean=0.0, std=1., p=0.5))
            elif item == 'Pe':
                augment_list.append(K.RandomPerspective(distortion_scale=0.5, p=0.7))
            elif item == 'Ro':
                augment_list.append(K.RandomRotation(degrees=15, p=0.7))
            elif item == 'Af':
                augment_list.append(K.RandomAffine(degrees=30, translate=0.1, shear=5, p=0.7, padding_mode='zeros', keepdim=True)) # border, reflection, zeros
            elif item == 'Et':
                augment_list.append(K.RandomElasticTransform(p=0.7))
            elif item == 'Ts':
                augment_list.append(K.RandomThinPlateSpline(scale=0.8, same_on_batch=True, p=0.7))
            elif item == 'Cr':
                augment_list.append(K.RandomCrop(size=(self.cut_size,self.cut_size), pad_if_needed=True, padding_mode='reflect', p=0.5))
            elif item == 'Er':
                augment_list.append(K.RandomErasing(scale=(.1, .4), ratio=(.3, 1/.3), same_on_batch=True, p=0.7))
            elif item == 'Re':
                augment_list.append(K.RandomResizedCrop(size=(self.cut_size,self.cut_size), scale=(0.1,1),  ratio=(0.75,1.333), cropping_mode='resample', p=0.5))
                
        self.augs = nn.Sequential(*augment_list)

    @autocast(enabled=use_mixed_precision)
    def forward(self, input):
        sideY, sideX = input.shape[2:4]
        max_size = min(sideX, sideY)
        min_size = min(sideX, sideY, self.cut_size)
        cutouts = []
        for _ in range(self.cutn):
            size = int(torch.rand([])**self.cut_pow * (max_size - min_size) + min_size)
            offsetx = torch.randint(0, sideX - size + 1, ())
            offsety = torch.randint(0, sideY - size + 1, ())
            cutout = input[:, :, offsety:offsety + size, offsetx:offsetx + size]
            cutouts.append(imageUtils.resample(cutout, (self.cut_size, self.cut_size), deterministic=deterministic))
        batch = self.augs(torch.cat(cutouts, dim=0))
        if self.noise_fac:
            facs = batch.new_empty([self.cutn, 1, 1, 1]).uniform_(0, self.noise_fac)
            batch = batch + facs * torch.randn_like(batch)

        return batch, [] #TODO, make this return cordinates for masking a well


# This is the original version (No pooling)
# original
class MakeCutoutsOrig(nn.Module):
    def __init__(self, cut_size, cutn, cut_pow=1., augments=[]):
        super().__init__()
        self.cut_size = cut_size
        self.cutn = cutn
        self.cut_pow = cut_pow

    @autocast(enabled=use_mixed_precision)
    def forward(self, input):
        sideY, sideX = input.shape[2:4]
        max_size = min(sideX, sideY)
        min_size = min(sideX, sideY, self.cut_size)
        cutouts = []
        for _ in range(self.cutn):
            size = int(torch.rand([])**self.cut_pow * (max_size - min_size) + min_size)
            offsetx = torch.randint(0, sideX - size + 1, ())
            offsety = torch.randint(0, sideY - size + 1, ())
            cutout = input[:, :, offsety:offsety + size, offsetx:offsetx + size]
            cutouts.append(imageUtils.resample(cutout, (self.cut_size, self.cut_size), deterministic=deterministic))

        return clamp_with_grad(torch.cat(cutouts, dim=0), 0, 1), [] #TODO, make this return cordinates for masking a well















####################################################
### start adding new cutout types and tests here ###
####################################################

class MakeCutoutsGrowFromCenter(MakeCutoutsSquish):
    def __init__(self, clipRes, cut_size_x, cut_size_y, cutn, cut_pow=1., use_pool=True, augments=[]):
        super().__init__(clipRes, cut_size_x, cut_size_y, cutn, cut_pow, False, augments)

        self.iterations = 0

    @autocast(enabled=use_mixed_precision)
    def forward(self, input):
        self.iterations = self.iterations + 1

        # seems to sharpen up everything after a sort of 'layout' is established
        if self.iterations > 100:
            return super().forward(input)

        sideY, sideX = input.shape[2:4]

        max_size_x = sideX
        max_size_y = sideY

        min_size_x = min(sideX, self.cut_size_x)
        min_size_y = min(sideX, self.cut_size_y)

        midX = max_size_x / 2
        midY = max_size_y / 2

        step_x = midX / self.cutn
        step_y = midY / self.cutn

        cutouts = []
        
        for i in range(self.cutn):            
            # create the cut
            # steps = i + 1
            steps = int(self.cutn / 2)

            size_x = int(2 * ( step_x * steps ))
            size_y = int(2 * ( step_y * steps ))

            xpos = int(midX - ( step_x * steps ))
            ypos = int(midY - ( step_y * steps ))

            cutout = input[:, :, xpos:xpos + size_y, ypos:ypos + size_x]

            if self.use_pool:
                cutout = (self.av_pool(cutout) + self.max_pool(cutout))/2
                cutouts.append(cutout)
            else:
                cutouts.append(imageUtils.resample(cutout, (self.clipRes, self.clipRes), deterministic=deterministic))            
            
        batch = self.augs(torch.cat(cutouts, dim=0))
        
        if self.noise_fac:
            facs = batch.new_empty([self.cutn, 1, 1, 1]).uniform_(0, self.noise_fac)
            batch = batch + facs * torch.randn_like(batch)

        return batch, [] #TODO, make this return cordinates for masking a well



## test method for masking attempts
class MakeCutoutsOneSpot(MakeCutoutsSquish):
    def __init__(self, clipRes, cut_size_x, cut_size_y, cutn, cut_pow=1., use_pool=True, augments=[]):
        super().__init__(clipRes, cut_size_x, cut_size_y, cutn, cut_pow, False, augments)

        self.iterations = 0

    @autocast(enabled=use_mixed_precision)
    def forward(self, input):
        self.iterations = self.iterations + 1

        # seems to sharpen up everything after a sort of 'layout' is established
        sideY, sideX = input.shape[2:4]

        max_size_x = sideX
        max_size_y = sideY

        midX = max_size_x / 2
        midY = max_size_y / 2

        cutouts = []

        size_x = self.clipRes
        size_y = self.clipRes

        xpos = int(midX - ( size_x / 2 ))
        ypos = int(midY - ( size_y / 2 ))

        #interesting - one cut still causes the rest of the image to go towards the prompt
        #but mostly just texturally / abstractly
        #the cut itself becomes more clearly the prompt
        #looks cool with styles
        #cutout = input[:, :, 0:xpos + size_y, ypos:ypos + size_x]
        cutout = input[:, :, 0:size_y, 0:size_x]

        # now pool for some reason? dont know what i'm doing but the results are good...
        if self.use_pool:
            cutout = (self.av_pool(cutout) + self.max_pool(cutout))/2
            cutouts.append(cutout)
        else:
            cutouts.append(imageUtils.resample(cutout, (self.clipRes, self.clipRes), deterministic=deterministic))            
            


        batch = self.augs(torch.cat(cutouts, dim=0))
        
        if self.noise_fac:
            facs = batch.new_empty([self.cutn, 1, 1, 1]).uniform_(0, self.noise_fac)
            batch = batch + facs * torch.randn_like(batch)

        return batch, [] #TODO, make this return cordinates for masking a well








class MakeCutoutsMaskTest(nn.Module):
    def __init__(self, clipRes, cut_size_x, cut_size_y, cutn, cut_pow=1., use_pool=True, augments=[]):
        super().__init__()
        self.cut_size_x = cut_size_x
        self.cut_size_y = cut_size_y
        self.cutn = cutn
        self.cut_pow = cut_pow # not used with pooling
        self.use_pool = use_pool
        self.clipRes = clipRes
        
        #self.augs = setupAugmentList(cut_size_x, cut_size_y)
        self.augs = setupAugmentList(augments, self.clipRes, self.clipRes)

        self.noise_fac = 0.1
        # self.noise_fac = False

        # Pooling
        self.av_pool = nn.AdaptiveAvgPool2d((self.clipRes, self.clipRes))
        self.max_pool = nn.AdaptiveMaxPool2d((self.clipRes, self.clipRes))

    @autocast(enabled=use_mixed_precision)
    def forward(self, input):
        sideY, sideX = input.shape[2:4]

        max_size_x = sideX
        max_size_y = sideY

        min_size_x = min(sideX, self.cut_size_x)
        min_size_y = min(sideX, self.cut_size_y)

        cutouts = []
        cutout_coords = []
        
        for _ in range(self.cutn):            
            # Use Pooling and original method together

            size_x = int(torch.rand([])**self.cut_pow * (max_size_x - min_size_x) + min_size_x)
            size_y = int(torch.rand([])**self.cut_pow * (max_size_y - min_size_y) + min_size_y)

            offsetx = torch.randint(0, sideX - size_x + 1, ())
            offsety = torch.randint(0, sideY - size_y + 1, ())

            cutout = input[:, :, offsety:offsety + size_y, offsetx:offsetx + size_x]
            cutout_coords.append([offsetx,offsetx + size_x,offsety,offsety + size_y])

            # now pool for some reason? dont know what i'm doing but the results are good...
            if self.use_pool:
                cutout = (self.av_pool(cutout) + self.max_pool(cutout))/2
                cutouts.append(cutout)
            else:
                cutouts.append(imageUtils.resample(cutout, (self.clipRes, self.clipRes), deterministic=deterministic))            
            
        batch = self.augs(torch.cat(cutouts, dim=0))
        
        if self.noise_fac:
            facs = batch.new_empty([self.cutn, 1, 1, 1]).uniform_(0, self.noise_fac)
            batch = batch + facs * torch.randn_like(batch)

        ## lets attempt to apply masked shit here i guess?


        return batch, cutout_coords      
